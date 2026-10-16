"""Real SDK transport tests for Qwen3.5 JSON Object mode, without network calls."""

from dataclasses import asdict
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI
from openai import APITimeoutError, AuthenticationError, RateLimitError
from pydantic import ValidationError

from app.agents.relevance import RelevanceAssessor, RelevanceOutputError
from app.agents.report_grounding import ReportGroundingChecker
from app.agents.reresearch_planner import ReResearchPlanner
from app.agents.supervisor import Supervisor
from app.agents.verifier import ClaimVerifier, VerificationOutputError
from app.agents.writer import ResearchWriter, WriterOutputError
from app.config import Settings
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import EvidenceExtractor, prepare_extraction_payload
from app.research.evidence_store import EvidenceStore
from app.research.reresearch import select_gap_groups
from app.research.reresearch_budget import ReResearchBudgetExhausted, ReResearchCallBudget, use_call_budget
from app.research.runtime import ResearchRuntime, ResearchTimeoutError
from app.research.writing_context import WritingClaim, WritingContext
from app.schemas import Claim, Evidence, Source, ResearchTask, ResearchPlan, ResearchGap, ResearchReport
from app.schemas.evidence_extraction import ExtractedEvidenceResult
from app.schemas.verification import BatchVerificationResult
from app.structured_output import (SchemaEchoError, json_instance_instructions, json_object_options,
                                   parse_json_instance, prefers_json_object)


BASE = 'https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
FACT = 'SQLite stores research checkpoints on disk.'


def completion(data, *, finish='stop'):
    return httpx.Response(200, json={"id":"test", "object":"chat.completion", "created":0, "model":"qwen3.5-flash",
        "choices":[{"index":0,"finish_reason":finish,"message":{"role":"assistant","content":json.dumps(data)}}],
        "usage":{"prompt_tokens":100,"completion_tokens":30,"total_tokens":130}})


def model_for(client, *, name='qwen3.5-flash', base=BASE):
    return ChatOpenAI(model=name, api_key='test-key', base_url=base, http_client=client, max_retries=0,
                      timeout=20, extra_body={"enable_thinking":True,"keep":"original"})


def graph():
    store = EvidenceStore()
    source=store.add_source(Source(source_id='src1', task_id='D', url='https://example.org/source', title='Source'))
    ev=store.add_evidence(Evidence(evidence_id='ev1', source_id=source.source_id, task_id='D', content=FACT))
    claim=store.add_claim(Claim(claim_id='claim1', task_id='D', text=FACT, evidence_ids=[ev.evidence_id]))
    task=ResearchTask(id='D', title='Checkpoint persistence', question='How are checkpoints persisted?', description='Checkpoint persistence')
    return store, claim, task


def writing_context():
    item=WritingClaim('claim1', FACT, ('ev1',), ('src1',), 'D')
    payload={"goal":"Checkpoint persistence", "scope_dimensions":["persistence"], "accepted_claims":[asdict(item)],
        "evidence":{"ev1":{"source_id":"src1","content":FACT}},
        "sources":{"src1":{"title":"Source","url":"https://example.org/source"}},
        "open_gaps":[],"partial_claims_for_limitations_only":[]}
    return WritingContext(payload['goal'],(item,),json.dumps(payload))


def report(text=FACT):
    paragraph={"text":text,"claim_ids":["claim1"]}
    return ResearchReport(title='Research report',scope='Checkpoint persistence',executive_summary=[paragraph],
        key_findings=[paragraph],sections=[{"heading":"Research findings","paragraphs":[paragraph]}],
        limitations=[],conclusion=[paragraph])


def assert_object_request(request):
    body=json.loads(request.content)
    assert body['response_format']=={"type":"json_object"}
    assert body['enable_thinking'] is False and body['keep']=='original'
    assert not body.get('tools')
    assert 'JSON DATA INSTANCE' in body['messages'][0]['content']
    return body


@pytest.mark.parametrize('name,base,expected',[
    ('qwen3.5-flash',BASE,True),('qwen3.5-flash-2026-02-23',BASE,True),
    ('qwen3.5-plus','https://dashscope.aliyuncs.com/compatible-mode/v1',True),
    ('qwen3.7-flash',BASE,False),('qwen3.5-flash','https://example.org/v1',False),
    ('Qwen/Qwen2.5-7B-Instruct','https://api.siliconflow.cn/v1',False),
])
def test_provider_and_model_scoped_capability(name,base,expected):
    model=SimpleNamespace(model_name=name,openai_api_base=base,extra_body={'keep':'original'})
    assert prefers_json_object(model) is expected
    options=json_object_options(model)
    assert bool(options) is expected and model.extra_body=={'keep':'original'}


@pytest.mark.parametrize('thinking',[False,True])
def test_extraction_skips_rejected_schema_request_and_forces_non_thinking(monkeypatch,thinking):
    monkeypatch.setattr('app.research.evidence_extraction.get_settings',lambda:Settings(_env_file=None,evidence_extraction_enable_thinking=thinking))
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        assert body['max_completion_tokens']==8192
        return completion({'selected_evidence_indexes':[0], 'claims':[{'text':FACT,'supporting_evidence_indexes':[0]}]})
    _,_,task=graph()
    payload=prepare_extraction_payload(task,'Synthetic adapter test',[{'url':'https://example.org/source','title':'Source',
        'text':FACT+' Further discussion.'}],EvidenceContext())
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=model_for(client)
        budget=ReResearchCallBudget(1)
        with use_call_budget(budget):
            result=EvidenceExtractor(model).extract(payload,ResearchRuntime('D',120,15))
        assert model.extra_body=={'enable_thinking':True,'keep':'original'}
    assert result.claims and len(requests)==budget.calls==1


@pytest.mark.parametrize('stage',['verification','relevance'])
def test_decisions_one_object_request_preserve_usage_and_budget(stage):
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        assert body['max_completion_tokens']==3072
        return completion({'results':[{'claim_id':'C1','status':'SUPPORTED' if stage=='verification' else 'ALIGNED','reason':'Directly supported'}]})
    store,claim,task=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=model_for(client)
        worker=ClaimVerifier(model,settings=Settings(_env_file=None)) if stage=='verification' else RelevanceAssessor(model,settings=Settings(_env_file=None))
        budget=ReResearchCallBudget(1)
        with use_call_budget(budget):
            result=worker.verify_batch([claim],store) if stage=='verification' else worker.assess_batch(task.question,[task],[claim])
        assert model.extra_body['enable_thinking'] is True
    assert len(result)==1 and worker.last_usage.llm_calls==len(requests)==budget.calls==1
    assert worker.last_usage.input_tokens==100 and worker.last_usage.output_tokens==30


def test_supervisor_uses_two_calls_without_a_rejected_native_attempt():
    requests=[]
    data={'goal':'Checkpoint persistence','tasks':[
        {'id':'D','title':'Find','question':'Find checkpoint persistence','description':'Find','task_type':'discovery','depends_on':[]},
        {'id':'S','title':'Summarize','question':'Summarize checkpoint persistence','description':'Summarize','task_type':'synthesis','depends_on':['D']}]}
    def transport(request):
        body=json.loads(request.content); requests.append(body)
        if len(requests)==1:
            return completion('Find how checkpoints persist')
        assert_object_request(request)
        return completion(data)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        result=Supervisor(model_for(client)).create_plan('Checkpoint persistence')
    assert len(result.tasks)==2 and len(requests)==2


def test_supplemental_planner_one_request_with_one_permit():
    requests=[]
    def transport(request):
        requests.append(assert_object_request(request))
        return completion({'results':[{'group_id':'G1','title':'Find direct details','description':'Find direct evidence about checkpoint persistence'}]})
    _,_,task=graph()
    plan=ResearchPlan(goal='Checkpoint persistence',tasks=[task])
    gap=ResearchGap(gap_type='COVERAGE_GAP',task_id='D',description='Missing details',missing_information='Direct evidence')
    settings=Settings(_env_file=None)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        budget=ReResearchCallBudget(1)
        with use_call_budget(budget):
            worker=ReResearchPlanner(model_for(client),settings=settings)
            result=worker.create_plan(plan.goal,plan,select_gap_groups([gap],settings),[])
    assert len(result.tasks)==1 and worker.last_calls==budget.calls==len(requests)==1


def test_writer_and_grounding_both_use_object_mode():
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        data=json.loads(body['messages'][1]['content'])
        if 'items' in data:
            return completion({'results':[{'item_index':item['item_index'],'status':'SUPPORTED','reason':'Supported paraphrase'} for item in data['items']]})
        result = report('SQLite saves research checkpoints on disk.').model_dump()
        for item in [*result['executive_summary'],*result['key_findings'],*result['conclusion'],
                     *(item for section in result['sections'] for item in section['paragraphs'])]:
            item['claim_ids']=['C1']
        return completion(result)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=model_for(client)
        gate=ReportGroundingChecker(model,settings=Settings(_env_file=None))
        writer=ResearchWriter(model,settings=Settings(_env_file=None),grounding_checker=gate)
        result=writer.write(writing_context())
        assert model.extra_body['enable_thinking'] is True
    assert result.used_claim_ids==['claim1'] and len(requests)==2
    assert writer.last_usage.llm_calls==1 and gate.last_usage.llm_calls==1
    assert not gate.last_usage.fallback_reason


@pytest.mark.parametrize('invalid_id',['P1','fabricated1'])
def test_writer_object_mode_rejects_partial_or_unknown_factual_references(invalid_id):
    context=writing_context()
    payload=context.payload
    payload['partial_claims_for_limitations_only']=[{'claim_id':'partial1','text':'Incomplete result'}]
    context=WritingContext(context.goal,context.accepted_claims,json.dumps(payload))
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        prompt=body['messages'][0]['content']
        assert '"claim_ids":["C1"]' in prompt
        assert '"partial_claim_ids":["P1"]' in prompt
        data=report().model_dump()
        for item in [*data['executive_summary'],*data['key_findings'],*data['conclusion'],
                     *(item for section in data['sections'] for item in section['paragraphs'])]:
            item['claim_ids']=['C1']
        data['conclusion'][0]['claim_ids']=[invalid_id]
        return completion(data)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        writer=ResearchWriter(model_for(client),settings=Settings(_env_file=None))
        result=writer.write(context)
    assert result.used_claim_ids==['claim1'] and writer.last_usage.fallback_mode=='verbatim_claims'
    assert len(requests)==writer.last_usage.llm_calls==2
    assert writer.last_usage.grounding.llm_calls==0


@pytest.mark.parametrize('stage',['extraction','verification','relevance'])
def test_schema_echo_is_rejected_without_publishing_empty_results_or_retry(stage):
    requests=[]
    def transport(request):
        requests.append(assert_object_request(request))
        return completion({'$defs':{},'properties':{},'type':'object'})
    store,claim,task=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=model_for(client)
        with pytest.raises(SchemaEchoError,match='instead of result data'):
            if stage=='extraction':
                payload=prepare_extraction_payload(task,'Test',[{'url':'https://example.org/source','title':'Source','text':FACT+' More details.'}],EvidenceContext())
                EvidenceExtractor(model).extract(payload,ResearchRuntime('D',120,15))
            elif stage=='verification':
                ClaimVerifier(model,settings=Settings(_env_file=None)).verify_batch([claim],store)
            else:
                RelevanceAssessor(model,settings=Settings(_env_file=None)).assess_batch(task.question,[task],[claim])
    assert len(requests)==1


@pytest.mark.parametrize('stage',['verification','relevance'])
def test_wrong_decision_ids_still_fail_closed(stage):
    def transport(request):
        assert_object_request(request)
        return completion({'results':[{'claim_id':'C99','status':'SUPPORTED' if stage=='verification' else 'ALIGNED','reason':'Wrong identifier'}]})
    store,claim,task=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=model_for(client)
        with pytest.raises((VerificationOutputError,RelevanceOutputError),match='exactly one decision'):
            if stage=='verification':
                ClaimVerifier(model,settings=Settings(_env_file=None)).verify_batch([claim],store)
            else:
                RelevanceAssessor(model,settings=Settings(_env_file=None)).assess_batch(task.question,[task],[claim])


def test_object_mode_timeout_retry_remains_once_and_current_batch_only():
    requests=[]
    def transport(request):
        requests.append(assert_object_request(request))
        if len(requests)==1:
            raise httpx.ReadTimeout('test timeout',request=request)
        return completion({'results':[{'claim_id':'C1','status':'ALIGNED','reason':'Addresses persistence'}]})
    _,claim,task=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=RelevanceAssessor(model_for(client),settings=Settings(_env_file=None))
        budget=ReResearchCallBudget(2)
        with use_call_budget(budget):
            results=worker.assess_batch(task.question,[task],[claim])
    assert results and worker.last_usage.llm_calls==len(requests)==budget.calls==2


def test_authentication_error_does_not_downgrade_or_retry():
    requests=[]
    def transport(request):
        requests.append(assert_object_request(request))
        return httpx.Response(401,json={'error':{'message':'Invalid credentials','type':'authentication_error'}})
    store,claim,_=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(AuthenticationError):
            ClaimVerifier(model_for(client),settings=Settings(_env_file=None)).verify_batch([claim],store)
    assert len(requests)==1


def test_json_data_shape_prompt_and_strict_unknown_field_validation():
    prompt=json_instance_instructions(ExtractedEvidenceResult.model_json_schema())
    assert 'OUTPUT DATA SHAPE' in prompt and '"selected_evidence_indexes"' in prompt and 'VALIDATION RULES' in prompt
    assert parse_json_instance('```json\n{"selected_evidence_indexes":[],"claims":[]}\n```')=={'selected_evidence_indexes':[],'claims':[]}
    with pytest.raises(ValidationError):
        ExtractedEvidenceResult.model_validate(parse_json_instance('{"selected_evidence_indexes":[],"claims":[],"unexpected":true}'))


def test_report_shape_uses_exact_allowed_claim_ids_instead_of_aliases():
    schema=ResearchReport.model_json_schema()
    for name in ('ReportParagraph','Finding'):
        schema['$defs'][name]['properties']['claim_ids']['items']['enum']=['claim_exact_allowed_id']
    prompt=json_instance_instructions(schema)
    shape=json.loads(prompt.split('OUTPUT DATA SHAPE (illustration, not an answer):\n')[1].split('\nVALIDATION RULES')[0])
    assert shape['key_findings'][0]['claim_ids']==['claim_exact_allowed_id']
    assert shape['sections'][0]['paragraphs'][0]['claim_ids']==['claim_exact_allowed_id']
    assert 'REFERENCE WHITELISTS BY OUTPUT FIELD' in prompt
    assert '"claim_ids":["claim_exact_allowed_id"]' in prompt


def decision_data(body, *, status='SUPPORTED'):
    payload=json.loads(body['messages'][1]['content'])
    return {'results':[{'claim_id':item['claim_id'],'status':status,'reason':'Direct evidence.'}
                       for item in payload['claims']]}


@pytest.mark.parametrize('error_kind',['string_entry','long_reason'])
def test_verifier_repairs_only_the_invalid_batch_with_complete_input(error_kind,capsys):
    store,claim,_=graph()
    claims=[claim]+[Claim(claim_id=f'claim{i}',task_id='D',text=f'{FACT} Fact {i}.',evidence_ids=['ev1'])
                    for i in range(2,8)]
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        result=decision_data(body,status='PARTIAL' if len(requests)==3 else 'SUPPORTED')
        if len(requests)==2:
            if error_kind=='string_entry':
                result['results'][2]='claim_id'
            else:
                result['results'][0]['reason']='x'*401
        return completion(result)
    budget=ReResearchCallBudget(4)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=ClaimVerifier(model_for(client),settings=Settings(_env_file=None,max_verify_claims_per_batch=3))
        with use_call_budget(budget):
            decisions=worker.verify_batch(claims,store)
    assert len(decisions)==7 and worker.last_usage.batches==3
    assert len(requests)==worker.last_usage.llm_calls==budget.calls==4
    assert requests[1]['messages'][1]==requests[2]['messages'][1]
    assert 'FORMAT CORRECTION' in requests[2]['messages'][0]['content']
    assert 'FORMAT CORRECTION' not in requests[3]['messages'][0]['content']
    assert [item.status.value for item in decisions]==['SUPPORTED']*3+['PARTIAL']*3+['SUPPORTED']
    assert worker.last_usage.context_chars==sum(len(body['messages'][1]['content']) for body in requests)
    assert worker.last_usage.input_tokens==400 and worker.last_usage.output_tokens==120
    assert worker.last_usage.usage_responses==4
    output=capsys.readouterr().out
    assert 'batch 2/3' in output and 'format_retry=1/1' in output


@pytest.mark.parametrize('error_kind',['string_entry','long_reason','unknown_id'])
def test_verifier_repair_never_ignores_second_invalid_result(error_kind):
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        result=decision_data(body)
        if len(requests)==1 or error_kind=='string_entry':
            result['results'][0]='claim_id'
        elif error_kind=='long_reason':
            result['results'][0]['reason']='x'*401
        else:
            result['results'][0]['claim_id']='invented'
        return completion(result)
    store,claim,_=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=ClaimVerifier(model_for(client),settings=Settings(_env_file=None))
        with pytest.raises(VerificationOutputError):
            worker.verify_batch([claim],store)
    assert len(requests)==worker.last_usage.llm_calls==2


def test_verifier_format_repair_obeys_supplemental_budget():
    requests=[]
    def transport(request):
        requests.append(assert_object_request(request))
        return completion({'results':['claim_id']})
    store,claim,_=graph()
    budget=ReResearchCallBudget(1)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=ClaimVerifier(model_for(client),settings=Settings(_env_file=None))
        with use_call_budget(budget),pytest.raises(ReResearchBudgetExhausted):
            worker.verify_batch([claim],store)
    assert len(requests)==worker.last_usage.llm_calls==budget.calls==1


@pytest.mark.parametrize('error_kind',['auth','rate_limit','timeout','length'])
def test_verifier_transport_or_truncation_error_during_repair_is_not_retried(error_kind):
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        if len(requests)==1:
            return completion({'results':['claim_id']})
        if error_kind=='auth':
            return httpx.Response(401,json={'error':{'message':'Invalid key','type':'authentication_error'}})
        if error_kind=='rate_limit':
            return httpx.Response(429,json={'error':{'message':'Rate limited','type':'rate_limit_error'}})
        if error_kind=='timeout':
            raise httpx.ReadTimeout('Test timeout',request=request)
        return completion(decision_data(body),finish='length')
    store,claim,_=graph()
    errors={'auth':AuthenticationError,'rate_limit':RateLimitError,'timeout':APITimeoutError,'length':VerificationOutputError}
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=ClaimVerifier(model_for(client),settings=Settings(_env_file=None))
        with pytest.raises(errors[error_kind]):
            worker.verify_batch([claim],store)
    assert len(requests)==worker.last_usage.llm_calls==2


def test_verifier_repair_uses_remaining_deadline(monkeypatch):
    from app.research import runtime as runtime_module
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append((request,body))
        return completion({'results':['claim_id']} if len(requests)==1 else decision_data(body))
    original=BatchVerificationResult.model_validate
    def validate(data):
        try:
            return original(data)
        except ValidationError:
            runtime_module._ACTIVE_RUNTIME.get().started-=20
            raise
    monkeypatch.setattr(BatchVerificationResult,'model_validate',validate)
    store,claim,_=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=ClaimVerifier(model_for(client),settings=Settings(_env_file=None,verification_timeout_seconds=30))
        assert worker.verify_batch([claim],store)
    assert len(requests)==2
    assert 0<requests[1][0].extensions['timeout']['read']<=10


def test_verifier_does_not_send_repair_after_deadline(monkeypatch):
    from app.research import runtime as runtime_module
    requests=[]
    def transport(request):
        requests.append(assert_object_request(request))
        return completion({'results':['claim_id']})
    original=BatchVerificationResult.model_validate
    def validate(data):
        try:
            return original(data)
        except ValidationError:
            runtime_module._ACTIVE_RUNTIME.get().started-=120
            raise
    monkeypatch.setattr(BatchVerificationResult,'model_validate',validate)
    store,claim,_=graph()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=ClaimVerifier(model_for(client),settings=Settings(_env_file=None,verification_timeout_seconds=30))
        with pytest.raises(ResearchTimeoutError):
            worker.verify_batch([claim],store)
    assert len(requests)==worker.last_usage.llm_calls==1


def test_verifier_format_retry_allowance_is_per_batch():
    requests=[]
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        return completion({'results':['claim_id']} if len(requests)%2 else decision_data(body))
    store,claim,_=graph()
    other=Claim(claim_id='claim2',task_id='D',text=FACT,evidence_ids=['ev1'])
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        worker=ClaimVerifier(model_for(client),settings=Settings(_env_file=None,max_verify_claims_per_batch=1))
        assert len(worker.verify_batch([claim,other],store))==2
    assert len(requests)==worker.last_usage.llm_calls==4
    assert requests[0]['messages'][1]==requests[1]['messages'][1]
    assert requests[2]['messages'][1]==requests[3]['messages'][1]


def test_exact_batch_shape_shows_all_requested_labels():
    schema=BatchVerificationResult.model_json_schema()
    schema['properties']['results'].update(minItems=3,maxItems=3)
    schema['$defs']['VerificationDecision']['properties']['claim_id']['enum']=['C1','C2','C3']
    prompt=json_instance_instructions(schema)
    shape=json.loads(prompt.split('OUTPUT DATA SHAPE (illustration, not an answer):\n')[1].split('\nVALIDATION RULES')[0])
    assert [item['claim_id'] for item in shape['results']]==['C1','C2','C3']


@pytest.mark.parametrize('stage,reason_length',[('verification',400),('relevance',320)])
def test_longer_valid_reasons_do_not_spend_a_format_retry(stage,reason_length):
    requests=[]
    reason='x'*reason_length
    def transport(request):
        requests.append(assert_object_request(request))
        return completion({'results':[{'claim_id':'C1','status':'SUPPORTED' if stage=='verification' else 'ALIGNED',
                                       'reason':reason}]})
    store,claim,task=graph()
    budget=ReResearchCallBudget(1)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=model_for(client)
        worker=ClaimVerifier(model,settings=Settings(_env_file=None)) if stage=='verification' else RelevanceAssessor(model,settings=Settings(_env_file=None))
        with use_call_budget(budget):
            result=worker.verify_batch([claim],store) if stage=='verification' else worker.assess_batch(task.question,[task],[claim])
    assert result[0].reason==reason
    assert worker.last_usage.llm_calls==budget.calls==len(requests)==1


def test_longer_grounding_reason_does_not_force_a_conservative_fallback():
    requests=[]
    reason='x'*320
    def transport(request):
        body=assert_object_request(request); requests.append(body)
        items=json.loads(body['messages'][1]['content'])['items']
        return completion({'results':[{'item_index':item['item_index'],'status':'SUPPORTED','reason':reason} for item in items]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        gate=ReportGroundingChecker(model_for(client),settings=Settings(_env_file=None))
        original=report('SQLite saves research checkpoints on disk.')
        checked=gate.check(original,writing_context())
    assert len(requests)==gate.last_usage.llm_calls==1
    assert not gate.last_usage.fallback_reason and gate.last_usage.repaired_items==0
    assert checked.key_findings==original.key_findings
    assert any(item.reason==reason for item in gate.last_audit)
