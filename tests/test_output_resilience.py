"""Reproduce the reported invalid rows/IDs and verify bounded, traceable recovery."""

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.agents.writer import ResearchWriter, validate_report
from app.config import Settings
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import (EvidenceExtractor, build_evidence_bundle,
    prepare_extraction_payload, validate_extraction_rows)
from app.research.evidence_store import EvidenceStore
from app.research.runtime import ResearchRuntime
from app.research.writer_references import WriterReferenceError, WriterReferences
from app.research.writing_context import WritingContext
from app.schemas import ResearchReport, ResearchTask
from app.tools.fetch_webpage import fetch_webpage
from test_writer import fixture, ModelStub, report_data, label_report, settings


def test_reported_extraction_two_empty_support_rows_preserves_four_valid_claims(capsys):
    task=ResearchTask(id='A1',title='Analysis',question='How are checkpoints stored?',description='Analyze sources',task_type='analysis')
    payload=prepare_extraction_payload(task,'Research memo',[
        {'url':'https://example.org/paper','title':'Source','text':'Checkpoints are stored on disk. Recovery skips completed tasks. The database persists research state.'}],EvidenceContext())
    rows=[{'text':'Checkpoints are stored on disk.','supporting_evidence_indexes':[0]} for _ in range(4)]
    rows += [{'text':'Unsupported synthesis','supporting_evidence_indexes':[]} for _ in range(2)]
    requests=[]
    def transport(request):
        body=json.loads(request.content);requests.append(body)
        assert body['response_format']=={'type':'json_object'} and body['enable_thinking'] is False
        return httpx.Response(200,json={'id':'test','object':'chat.completion','created':0,'model':'qwen3.5-plus',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({'claims':rows})}}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=ChatOpenAI(model='qwen3.5-plus-2026-04-20',api_key='test',
            base_url='https://workspace.maas.aliyuncs.com/compatible-mode/v1',http_client=client,max_retries=0)
        extracted=EvidenceExtractor(model).extract(payload,ResearchRuntime('A1',120,15))
    assert len(extracted.claims)==4 and len(requests)==1
    bundle=build_evidence_bundle(task,extracted,payload,EvidenceContext())
    store=EvidenceStore();store.merge(bundle)
    assert all(store.get_sources_for_claim(item.claim_id) and item.evidence_ids for item in bundle.claims)
    output=capsys.readouterr().out
    assert 'row=4' in output and 'row=5' in output and 'too_short' in output


@pytest.mark.parametrize('row',[None,'claim_id',{'text':'missing refs'},
    {'text':'empty refs','supporting_evidence_indexes':[]},
    {'text':'bad refs','supporting_evidence_indexes':[True]},
    {'text':'extra','supporting_evidence_indexes':[0],'confidence':1}])
def test_invalid_claim_row_isolated_without_weakening_formal_contract(row):
    result=validate_extraction_rows({'claims':[{'text':'Valid','supporting_evidence_indexes':[0]},row]})
    assert len(result.claims)==1 and result.claims[0].supporting_evidence_indexes==[0]


def test_all_invalid_rows_become_explicit_coverage_gap(capsys):
    rows={'claims':[{'text':'Unsupported','supporting_evidence_indexes':[]},'claim_id']}
    assert validate_extraction_rows(rows,print).claims==[]
    assert 'coverage gap' in capsys.readouterr().out


@pytest.mark.parametrize('data',[{'claims':'not an array'},{'claims':[],'schema':{}}])
def test_bad_extraction_container_does_not_masquerade_as_no_findings(data):
    with pytest.raises(ValidationError):
        validate_extraction_rows(data)


def test_writer_disjoint_labels_roundtrip_without_modifying_canonical_context():
    *_,context=fixture([('SUPPORTED','ALIGNED'),('PARTIAL','ALIGNED')])
    payload=context.payload
    payload['open_gaps']=[{'gap_id':'gap_original','status':'OPEN','gap_type':'COVERAGE_GAP','missing_information':'Details'}]
    context=WritingContext(context.goal,context.accepted_claims,json.dumps(payload))
    before=context.text
    refs=WriterReferences.build(context)
    encoded=json.loads(refs.text)
    assert encoded['accepted_claims'][0]['claim_id']=='C1'
    assert encoded['partial_claims_for_limitations_only'][0]['claim_id']=='P1'
    assert encoded['open_gaps'][0]['gap_id']=='G1'
    data=label_report(report_data(context),context)
    data['limitations'][0]['partial_claim_ids']=['P1']
    decoded=refs.decode(ResearchReport.model_validate(data))
    validate_report(decoded,context)
    assert decoded.used_claim_ids==[context.accepted_claims[0].claim_id]
    assert decoded.limitations[0].gap_ids==['gap_original'] and decoded.limitations[0].partial_claim_ids==list(refs.partial.values())
    assert context.text==before


@pytest.mark.parametrize('label',['P1','G1','C99','canonical'])
def test_wrong_writer_reference_never_binds_to_a_different_claim(label):
    *_,context=fixture([('SUPPORTED','ALIGNED'),('PARTIAL','ALIGNED')])
    refs=WriterReferences.build(context)
    data=json.loads(json.dumps(label_report(report_data(context),context)))
    data['conclusion'][0]['claim_ids']=[context.accepted_claims[0].claim_id if label=='canonical' else label]
    with pytest.raises(WriterReferenceError,match='conclusion.0.claim_ids'):
        refs.decode(ResearchReport.model_validate(data))


def test_writer_one_correction_returns_valid_draft_without_research():
    *_,context=fixture()
    calls=[]
    def mutate(data):
        calls.append(1)
        if len(calls)==1:
            data['conclusion'][0]['claim_ids']=['P1']
        return data
    model=ModelStub(context,mutate=mutate)
    writer=ResearchWriter(model,settings=settings())
    # The gate here checks known prose through the existing conservative rules.
    from test_writer import ApprovingGroundingModel
    from app.agents.report_grounding import ReportGroundingChecker
    writer.grounding_checker=ReportGroundingChecker(ApprovingGroundingModel(),settings=settings())
    result=writer.write(context)
    assert result.used_claim_ids and not writer.last_usage.fallback_mode
    assert writer.last_usage.llm_calls==2 and writer.last_usage.format_retries==1
    assert model.requests[0][0][1]==model.requests[1][0][1]
    assert 'FORMAT CORRECTION' in model.requests[1][0][0].content
    assert writer.last_usage.input_tokens==440 and writer.last_usage.output_tokens==200


def test_failed_writer_drafts_are_discarded_and_literal_fallback_has_complete_citations():
    store,*_,context=fixture([('SUPPORTED','ALIGNED'),('PARTIAL','ALIGNED')])
    def mutate(data):
        data['conclusion'][0]={'text':'Invented SOTA performance','claim_ids':['P1']}
        return data
    writer=ResearchWriter(ModelStub(context,mutate=mutate),settings=settings())
    result=writer.write(context)
    from app.research.report_rendering import render_report_with_citations
    rendered=render_report_with_citations(result,store)
    assert writer.last_usage.llm_calls==2 and writer.last_usage.grounding.llm_calls==0
    assert writer.last_usage.fallback_mode=='verbatim_claims'
    assert all(item.text in {claim.text for claim in context.accepted_claims} for item in result.factual_paragraphs())
    assert result.used_claim_ids==[context.accepted_claims[0].claim_id]
    assert 'Invented SOTA' not in rendered.markdown
    assert rendered.references and all(item.status=='SUPPORTED' for item in writer.last_grounding)


@pytest.mark.parametrize('bad_content',['not json','{"$defs":{},"type":"object","properties":{}}'])
def test_invalid_json_or_schema_echo_has_bounded_writer_fallback(bad_content):
    *_,context=fixture()
    class BadModel:
        def with_structured_output(self,*args,**kwargs):raise NotImplementedError
        def invoke(self,*args,**kwargs):return AIMessage(content=bad_content)
    writer=ResearchWriter(BadModel(),settings=settings())
    assert writer.write(context).used_claim_ids
    assert writer.last_usage.llm_calls==2 and writer.last_usage.fallback_mode=='verbatim_claims'


def test_unpublishable_literal_claim_keeps_material_and_returns_insufficient_report():
    *_,context=fixture()
    original=context.accepted_claims[0]
    long_claim=replace(original,text='x'*2200)
    payload=context.payload;payload['accepted_claims'][0]['text']=long_claim.text
    context=WritingContext(context.goal,(long_claim,),json.dumps(payload))
    writer=ResearchWriter(ModelStub(context,mutate=lambda data:{'broken':'draft'}),settings=settings())
    report=writer.write(context)
    assert writer.last_usage.fallback_mode=='insufficient_material'
    assert writer.last_usage.omitted_claim_ids==[original.claim_id] and not report.used_claim_ids
    assert context.accepted_claims[0]==long_claim
    validate_report(report,context,allow_insufficient=True)
    with pytest.raises(ValueError):validate_report(report,context)


@pytest.mark.parametrize('body',['游戏工委','Just a moment. Checking your browser before proceeding.',
                               'Please enable JavaScript to continue viewing this website.'])
def test_tiny_or_browser_challenge_html_is_failed_material(monkeypatch,body):
    import importlib
    module=importlib.import_module('app.tools.fetch_webpage')
    response=SimpleNamespace(url='https://example.org/page',headers={'Content-Type':'text/html'},
        content=('<html><body><main>'+body+'</main></body></html>').encode(),raise_for_status=lambda:None)
    monkeypatch.setattr(module.requests,'get',lambda *args,**kwargs:response)
    result=json.loads(fetch_webpage.invoke({'url':response.url}))
    assert 'error' in result and 'text' not in result


def test_body_validation_precedes_requested_excerpt_truncation(monkeypatch):
    import importlib
    module=importlib.import_module('app.tools.fetch_webpage')
    response=SimpleNamespace(url='https://example.org/page',headers={'Content-Type':'text/html'},
        content=b'<html><body><main>Checkpoints are stored in SQLite and preserve research state.</main></body></html>',raise_for_status=lambda:None)
    monkeypatch.setattr(module.requests,'get',lambda *args,**kwargs:response)
    result=json.loads(fetch_webpage.invoke({'url':response.url,'max_chars':4}))
    assert result['text']=='Chec'


def test_semantic_gate_failure_uses_safe_fallback_without_another_writer_request():
    *_,context=fixture()
    from app.agents.report_grounding import ReportGroundingUsage
    gate=SimpleNamespace(last_usage=ReportGroundingUsage(llm_calls=1,input_tokens=100),last_audit=())
    def fail(*args):raise ValueError('No complete cited Claim fits generated grouping')
    gate.check=fail
    writer=ResearchWriter(ModelStub(context),settings=settings(),grounding_checker=gate)
    result=writer.write(context)
    assert writer.last_usage.fallback_mode=='verbatim_claims'
    assert writer.last_usage.llm_calls==1 and writer.last_usage.grounding.llm_calls==1
    assert result.used_claim_ids and writer.last_grounding


def test_fallback_usage_defaults_are_backward_compatible_with_saved_runs():
    from app.persistence.models import ResearchRunState
    old=ResearchRunState.model_validate({'goal':'Goal','writer_usage':{'llm_calls':1}})
    assert old.writer_usage.format_retries==0 and old.writer_usage.fallback_mode==''
    old.writer_usage.fallback_mode='verbatim_claims'
    old.writer_usage.fallback_reason='Invalid P label'
    saved=ResearchRunState.model_validate_json(old.model_dump_json())
    assert saved.writer_usage.fallback_mode=='verbatim_claims'
    assert saved.writer_usage.fallback_reason=='Invalid P label'
