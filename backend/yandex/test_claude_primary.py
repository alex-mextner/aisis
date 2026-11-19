import asyncio
import json
from pathlib import Path
import shlex
import tempfile
import unittest

import claude_primary as c


def key(n):
    return c.Credential('slot'+str(n), 'sk-ant-api03-fixture-'+str(n)*30)


def usr(n):
    return 'sk-ant-usr-'+'fixture'+str(n)*30


def request(turn='turn-1', station='a', text='Привет'):
    return dict(turn_id=turn, station_id=station, text=text, conversation_id=None)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=c.State(Path(self.temp.name)/'state.sqlite');self.addCleanup(self.state.close)
        self.calls=[]; self.pm=[]; self.effects={}; self.now=1000.0
        self.settings=c.Settings(allowed=frozenset({'a','b'}), history_pairs=0)
        async def send(credential, body, timeout):
            self.calls.append((credential.label,body,timeout))
            result=self.effects.get(credential.label,'Ответ')
            if isinstance(result,BaseException):raise result
            if callable(result):return await result()
            return result
        async def fallback(payload):
            self.pm.append(payload)
            return {'text':'Резервный ответ','conversation_id':'https://chatgpt.com/c/owned'}
        self.router=c.Router(self.settings,[key(1),key(2),key(3)],self.state,
                             send=send,fallback=fallback,clock=lambda:self.now)
    async def test_primary_success_never_calls_other_keys_or_browser(self):
        result=await self.router.submit(request())
        self.assertEqual(result['text'],'Ответ')
        self.assertEqual(result['provider'],'claude')
        self.assertEqual([x[0] for x in self.calls],['slot1']);self.assertEqual(self.pm,[])
    async def test_429_then_503_selects_third_key(self):
        self.effects={'slot1':c.Failover('rate_limit',30), 'slot2':c.Failover('server_error',15)}
        result=await self.router.submit(request())
        self.assertEqual(result['credential_slot'],'slot3')
        self.assertEqual([x[0] for x in self.calls],['slot1','slot2','slot3']);self.assertEqual(self.pm,[])
    async def test_all_keys_fail_then_pm_once(self):
        self.effects={f'slot{n}':c.Failover('unavailable',15) for n in (1,2,3)}
        result=await self.router.submit(request())
        self.assertEqual(result['provider'],'chatgpt_web');self.assertEqual(len(self.pm),1)
        self.assertEqual(self.pm[0]['turn_id'],'turn-1')
        self.assertEqual(self.pm[0]['text'],'Привет')
    async def test_duplicate_turn_is_never_resent_even_after_success(self):
        await self.router.submit(request())
        with self.assertRaisesRegex(c.RouterError,'duplicate_turn'):await self.router.submit(request())
        self.assertEqual(len(self.calls),1)
    async def test_duplicate_turn_is_never_resent_after_pm_failure(self):
        self.effects={f'slot{n}':c.Failover('unavailable',15) for n in (1,2,3)}
        async def stopped(payload):self.pm.append(payload);raise RuntimeError('pm_bridge_guardian_stopped')
        self.router.fallback=stopped
        with self.assertRaisesRegex(c.RouterError,'chatgpt_fallback_failed'):await self.router.submit(request())
        with self.assertRaisesRegex(c.RouterError,'duplicate_turn'):await self.router.submit(request())
        self.assertEqual(len(self.pm),1)
    async def test_cooldown_persists_across_router_instances(self):
        self.effects={'slot1':c.Failover('rate_limit',30)}
        await self.router.submit(request())
        self.state.close();self.state=c.State(Path(self.temp.name)/'state.sqlite');self.router.state=self.state
        self.calls.clear();await self.router.submit(request('turn-2'))
        self.assertEqual([x[0] for x in self.calls],['slot2'])
        self.now+=31;self.calls.clear();self.effects={}
        await self.router.submit(request('turn-3'));self.assertEqual(self.calls[0][0],'slot1')
    async def test_long_provider_retry_after_is_honoured_in_full_across_restart(self):
        async def limited():
            c.parse_response(429, {'Retry-After': '172800'}, {})
        self.effects={'slot1':limited}
        await self.router.submit(request())
        self.state.close();self.state=c.State(Path(self.temp.name)/'state.sqlite');self.router.state=self.state
        self.now+=86401;self.calls.clear();self.effects={}
        await self.router.submit(request('turn-2'))
        self.assertEqual([x[0] for x in self.calls],['slot2'])
        self.now+=172800-86401;self.calls.clear()
        await self.router.submit(request('turn-3'));self.assertEqual(self.calls[0][0],'slot1')
    async def test_bad_request_does_not_fall_through(self):
        self.effects={'slot1':c.RouterError('invalid_request')}
        with self.assertRaisesRegex(c.RouterError,'invalid_request'):await self.router.submit(request())
        self.assertEqual(len(self.calls),1);self.assertEqual(self.pm,[])
    async def test_unknown_station_rejected_before_network_or_record(self):
        with self.assertRaisesRegex(c.RouterError,'station_not_allowed'):await self.router.submit(request(station='evil'))
        self.assertEqual(self.calls,[]);self.assertEqual(self.state.count_turns(),0)
    async def test_cancelled_request_does_not_move_to_another_provider(self):
        async def cancelled():raise asyncio.CancelledError()
        self.effects={'slot1':cancelled}
        with self.assertRaises(asyncio.CancelledError):await self.router.submit(request())
        self.assertEqual(len(self.calls),1);self.assertEqual(self.pm,[])
        self.assertEqual(self.state.turn_status('turn-1'),'uncertain')
    async def test_old_pm_url_is_preserved_by_claude(self):
        payload=request();payload['conversation_id']='https://chatgpt.com/c/legacy'
        result=await self.router.submit(payload)
        self.assertEqual(result['conversation_id'],payload['conversation_id'])
    async def test_default_does_not_persist_transcript(self):
        await self.router.submit(request(text='UniqueSecretPrompt123'))
        self.assertNotIn(b'UniqueSecretPrompt123',(Path(self.temp.name)/'state.sqlite').read_bytes())
    async def test_opt_in_context_is_bounded_and_isolated(self):
        self.router.settings=c.Settings(allowed=frozenset({'a','b'}),history_pairs=2)
        await self.router.submit(request('a1',text='Первое'))
        await self.router.submit(request('a2',text='Второе'))
        await self.router.submit(request('b1','b','Другое'))
        self.assertEqual(len(self.calls[-1][1]['messages']),1)
        await self.router.submit(request('a3',text='Третье'))
        messages=self.calls[-1][1]['messages']
        self.assertEqual([m['content'] for m in messages if m['role']=='user'],['Первое','Второе','Третье'])
        self.assertEqual(len(self.state.context('a',2,1000)),4)
    async def test_all_cooling_down_calls_only_pm(self):
        for n in (1,2,3):self.state.cooldown(key(n).fingerprint,1030)
        result=await self.router.submit(request())
        self.assertEqual(self.calls,[]);self.assertEqual(result['provider'],'chatgpt_web')
    async def test_context_passed_to_pm_does_not_replace_old_conversation(self):
        self.router.settings=c.Settings(allowed=frozenset({'a','b'}),history_pairs=2)
        await self.router.submit(request('one',text='Запомни маяк'))
        self.effects={f'slot{n}':c.Failover('unavailable',15) for n in (1,2,3)}
        p=request('two',text='Какое слово?');p['conversation_id']='https://chatgpt.com/c/legacy'
        await self.router.submit(p)
        self.assertIn('Запомни маяк',self.pm[0]['text']);self.assertIn('Какое слово?',self.pm[0]['text'])
        self.assertEqual(self.pm[0]['conversation_id'],p['conversation_id'])
    async def test_new_api_key_is_not_blocked_by_old_key_cooldown(self):
        self.state.cooldown(key(1).fingerprint,1100)
        replacement=c.Credential('slot1','sk-ant-api03-replacement-'+ 'z'*30)
        self.router.credentials=[replacement,key(2),key(3)]
        await self.router.submit(request());self.assertEqual(self.calls[0][0],'slot1')
    async def test_request_body_no_temperature_tools_or_hidden_retry(self):
        await self.router.submit(request())
        b=self.calls[0][1]
        self.assertEqual(b['model'],'claude-sonnet-5-5')
        self.assertEqual(b['thinking'],{'type':'between_tools'})
        self.assertEqual(b['output_config'],{'effort':'medium'})
        self.assertNotIn('tools',b);self.assertNotIn('temperature',b)
    async def test_history_expires_on_next_request(self):
        self.router.settings=c.Settings(allowed=frozenset({'a','b'}),history_pairs=2,history_ttl=60)
        await self.router.submit(request('one'));self.now+=61
        await self.router.submit(request('two'));self.assertEqual(len(self.calls[-1][1]['messages']),1)
    async def test_disabling_history_purges_every_speaker_not_only_the_current_one(self):
        both=frozenset({'a','b'})
        self.router.settings=c.Settings(allowed=both,history_pairs=2)
        await self.router.submit(request('a1','a','ПриватныйПромптА'))
        await self.router.submit(request('b1','b','ПриватныйПромптБ'))
        self.router.settings=c.Settings(allowed=both,history_pairs=0)
        await self.router.submit(request('a2','a','Снова'))
        db=(Path(self.temp.name)/'state.sqlite').read_bytes()
        self.assertNotIn('ПриватныйПромптА'.encode(),db);self.assertNotIn('ПриватныйПромптБ'.encode(),db)
        self.router.settings=c.Settings(allowed=both,history_pairs=2)
        await self.router.submit(request('b2','b','Новый'))
        self.assertEqual(len(self.calls[-1][1]['messages']),1)
    async def test_pm_context_is_single_line_and_fits_the_pm_request_limit(self):
        self.router.settings=c.Settings(allowed=frozenset({'a','b'}),history_pairs=4)
        for n in range(3):
            await self.router.submit(request(f'h{n}','a','😀'*4000))
        self.effects={f'slot{n}':c.Failover('unavailable',15) for n in (1,2,3)}
        await self.router.submit(request('q','a','Короткий вопрос\nс переводом строки'))
        sent=self.pm[0]
        # The browser composer sends single-line text only, and the PM server rejects bodies over 128 KiB.
        self.assertNotIn('\n',sent['text'])
        self.assertLessEqual(len(json.dumps(sent).encode()),120000)
        self.assertIn('Короткий вопрос',sent['text'])


class ClassificationTests(unittest.TestCase):
    def response(self,text='OK',stop='end_turn'):
        return {'content':[{'type':'text','text':text}], 'stop_reason':stop}
    def test_200_parses_text(self):self.assertEqual(c.parse_response(200,{},self.response()),'OK')
    def test_refusal_is_answer_not_failover(self):
        self.assertEqual(c.parse_response(200,{},self.response('Не могу помочь','refusal')),'Не могу помочь')
    def test_empty_refusal_gets_spoken_refusal_without_other_provider(self):
        self.assertTrue(c.parse_response(200,{},dict(content=[],stop_reason='refusal')))
    def test_truncated_text_is_spoken_but_an_empty_truncation_still_fails(self):
        self.assertEqual(c.parse_response(200,{},self.response('Начало длинного ответа','max_tokens')),'Начало длинного ответа')
        with self.assertRaisesRegex(c.RouterError,'incomplete_answer'):
            c.parse_response(200,{},self.response('','max_tokens'))
    def test_invalid_json_shape_and_tool_calls_fail_closed(self):
        for b in ([],{},dict(content=[{'type':'tool_use'}],stop_reason='end_turn')):
            with self.assertRaises(c.RouterError):c.parse_response(200,{},b)
    def test_429_honours_retry_after(self):
        with self.assertRaises(c.Failover) as e:c.parse_response(429,{'Retry-After':'120'}, {})
        self.assertGreaterEqual(e.exception.cooldown_seconds,120)
    def test_long_finite_retry_after_is_not_shortened(self):
        for header in ('172800','99999999999'):
            with self.subTest(header=header),self.assertRaises(c.Failover) as e:c.parse_response(429,{'Retry-After':header},{})
            self.assertEqual(e.exception.cooldown_seconds,float(header))
    def test_credential_and_billing_cooldowns_also_honour_longer_retry_after(self):
        billing={'error':{'type':'invalid_request_error','message':'Your credit balance is too low to access the Anthropic API.'}}
        for status,body,default in ((401,{},300),(403,{},300),(402,{},3600),(400,billing,3600)):
            with self.subTest(status=status),self.assertRaises(c.Failover) as e:c.parse_response(status,{'retry-after':'172800'},body)
            self.assertEqual(e.exception.cooldown_seconds,172800)
            with self.subTest(status=status,header='absent'),self.assertRaises(c.Failover) as e:c.parse_response(status,{},body)
            self.assertEqual(e.exception.cooldown_seconds,default)
    def test_malformed_or_non_finite_retry_after_uses_safe_default(self):
        for header in ('nan','inf','-inf','1e999','soon',''):
            with self.subTest(header=header),self.assertRaises(c.Failover) as e:c.parse_response(429,{'Retry-After':header},{})
            self.assertEqual(e.exception.cooldown_seconds,60)
    def test_transient_and_credential_errors_allow_failover(self):
        for status in (401,402,403,408,429,500,502,503,504,529):
            with self.subTest(status=status),self.assertRaises(c.Failover):c.parse_response(status,{}, {})
    def test_redirects_fail_closed(self):
        for status in (301,302):
            with self.subTest(status=status),self.assertRaises(c.RouterError) as e:c.parse_response(status,{}, {})
            self.assertNotIsInstance(e.exception,c.Failover)
    def test_other_client_rejections_try_the_next_key_instead_of_ending_the_turn(self):
        # Model access, workspace and organisation state are per key: a rejection of one key says nothing about the next.
        for status in (400,404,413,422):
            with self.subTest(status=status),self.assertRaises(c.Failover) as e:c.parse_response(status,{}, {})
            self.assertEqual(str(e.exception),'request_rejected');self.assertEqual(e.exception.cooldown_seconds,300)
    def test_disabled_organisation_cools_the_key_for_an_hour(self):
        body={'error':{'type':'invalid_request_error','message':'This organization has been disabled.'}}
        with self.assertRaises(c.Failover) as e:c.parse_response(400,{},body)
        self.assertEqual((str(e.exception),e.exception.cooldown_seconds),('billing_unavailable',3600))
    def test_documented_credit_balance_error_allows_next_key(self):
        body={'error':{'type':'invalid_request_error','message':'Your credit balance is too low to access the Anthropic API.'}}
        with self.assertRaises(c.Failover):c.parse_response(400,{},body)
    def test_prompt_text_in_error_is_not_exposed(self):
        body={'error':{'type':'invalid_request_error','message':'private prompt and sk-ant-api-secret'}}
        with self.assertRaises(c.RouterError) as e:c.parse_response(400,{},body)
        self.assertNotIn('private',str(e.exception));self.assertNotIn('sk-ant',str(e.exception))
    def test_timeout_configuration_is_finite_and_nested(self):
        for kwargs in ({'attempt_timeout':float('nan')},{'attempt_timeout':0},{'total_timeout':10}, {'fallback_timeout':301}):
            with self.assertRaises(c.RouterError):c.Settings(allowed=frozenset({'a'}),**kwargs).validate()
    def test_credential_repr_does_not_expose_secret(self):self.assertNotIn(key(1).secret,repr(key(1)))
    def test_deduplication_is_secret_based(self):
        keys=[key(1),c.Credential('other',key(1).secret),key(2)]
        self.assertEqual(len(c.unique_credentials(keys)),2)


class CredentialSourceTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def config(self,content,variables=('CLAUDE_API_TOKEN','CLAUDE_API_KEY','ANTHROPIC_API_KEY')):
        rows=[]
        for i,text in enumerate(content):
            p=self.root/f'bot{i}.env';p.write_text(text);p.chmod(0o600)
            rows.append({'label':f'slot{i+1}','path':str(p),'variables':list(variables)})
        f=self.root/'sources.json';f.write_text(json.dumps(rows));f.chmod(0o600);return f
    def test_sources_use_claude_token_not_zai_historical_name(self):
        f=self.config(['ANTHROPIC_API_KEY=zai_key\nCLAUDE_API_TOKEN='+key(1).secret])
        self.assertEqual(c.load_credentials(f)[0].secret,key(1).secret)
    def test_shell_expressions_are_never_executed(self):
        f=self.config(['CLAUDE_API_TOKEN=$(touch /tmp/not-run)'])
        self.assertEqual(c.load_credentials(f),[])
    def test_quotes_comments_and_export_supported(self):
        f=self.config([f'export CLAUDE_API_TOKEN="{key(1).secret}" # hi'])
        self.assertEqual(c.load_credentials(f)[0].secret,key(1).secret)
    def test_three_distinct_source_credentials_in_order(self):
        f=self.config([f'CLAUDE_API_TOKEN={key(n).secret}' for n in (1,2,3)])
        self.assertEqual([x.secret for x in c.load_credentials(f)],[key(n).secret for n in (1,2,3)])
    def test_duplicate_sources_not_counted_as_extra_keys(self):
        f=self.config([f'CLAUDE_API_TOKEN={key(1).secret}']*3)
        self.assertEqual(len(c.load_credentials(f)),1)
    def test_oauth_token_not_misused_as_api_key(self):
        f=self.config(['CLAUDE_API_TOKEN=sk-ant-oat01-'+ 'x'*40])
        self.assertEqual(c.load_credentials(f),[])
    def test_console_user_key_format_is_accepted(self):
        f=self.config(['CLAUDE_API_TOKEN='+usr(1)])
        self.assertEqual([x.secret for x in c.load_credentials(f)],[usr(1)])
    def test_unsafe_or_foreign_key_shapes_are_rejected(self):
        shapes=['sk-ant-oat01-'+'x'*40,'sk-ant-usr-short','sk-ant-userfixture'+'x'*40,
                'sk-ant-usr-'+'x'*20+'$HOME','sk-ant-usr-'+'x'*20+' extra','zai-'+'x'*60,
                'sk-ant-usr-'+'x'*257]
        for shape in shapes:
            with self.subTest(shape=shape[:18]):
                f=self.config(['CLAUDE_API_TOKEN='+shlex.quote(shape)])
                self.assertEqual(c.load_credentials(f),[])
    def test_second_token_variable_is_permitted_and_preferred(self):
        text=f'CLAUDE_API_TOKEN={usr(1)}\nCLAUDE_API_TOKEN2={usr(2)}\n'
        f=self.config([text],('CLAUDE_API_TOKEN2',))
        self.assertEqual([x.secret for x in c.load_credentials(f)],[usr(2)])
        f=self.config([text],('CLAUDE_API_TOKEN2','CLAUDE_API_TOKEN'))
        self.assertEqual([x.secret for x in c.load_credentials(f)],[usr(2)])
    def test_calendar_token2_gives_three_distinct_keys_with_finance_and_report(self):
        both=f'CLAUDE_API_TOKEN={usr(1)}\nCLAUDE_API_TOKEN2={usr(2)}\n'
        rows=[('finance','CLAUDE_API_TOKEN',both),('calendar','CLAUDE_API_TOKEN2',both),
              ('report','CLAUDE_API_KEY',f'CLAUDE_API_KEY={usr(3)}\n')]
        sources=[]
        for label,variable,text in rows:
            p=self.root/(label+'.env');p.write_text(text);p.chmod(0o600)
            sources.append({'label':label,'path':str(p),'variables':[variable]})
        f=self.root/'sources.json';f.write_text(json.dumps(sources));f.chmod(0o600)
        self.assertEqual([(x.label,x.secret) for x in c.load_credentials(f)],
                         [('finance',usr(1)),('calendar',usr(2)),('report',usr(3))])
    def test_duplicate_source_leaves_fewer_than_three_keys(self):
        f=self.config([f'CLAUDE_API_TOKEN={usr(1)}']*2+[f'CLAUDE_API_KEY={usr(3)}'])
        self.assertEqual([x.label for x in c.load_credentials(f)],['slot1','slot3'])
    def test_source_files_unchanged(self):
        f=self.config([f'CLAUDE_API_TOKEN={key(1).secret}'])
        before=(self.root/'bot0.env').read_bytes();c.load_credentials(f)
        self.assertEqual(before,(self.root/'bot0.env').read_bytes())
    def test_last_empty_assignment_revokes_earlier_key(self):
        f=self.config([f'CLAUDE_API_TOKEN={key(1).secret}\nCLAUDE_API_TOKEN='])
        self.assertEqual(c.load_credentials(f),[])
    def test_insecure_source_refused_before_using_key(self):
        f=self.config([f'CLAUDE_API_TOKEN={key(1).secret}'])
        (self.root/'bot0.env').chmod(0o664)
        with self.assertRaisesRegex(c.RouterError,'credential_source_not_private'):
            c.load_credentials(f)
    def test_database_permissions_private(self):
        s=c.State(self.root/'state.sqlite');s.close()
        self.assertEqual((self.root/'state.sqlite').stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
