"""Discovery sends only metadata requests to an explicitly selected issuer."""
import json
from unittest import TestCase
from unittest.mock import patch
import httpx
from app.model_discovery import discover, fetch_models, normalize_provider, project_models, provider_presets
from app.api_models import APIModelError
from tests.test_api_models import APIModelsTests

HTTPClient = httpx.Client

class ModelDiscoveryTests(TestCase):
    setUp = APIModelsTests.setUp
    add = APIModelsTests.add
    def test_presets_fill_address_and_reject_unknown_issuer(self):
        row = normalize_provider({'provider':'groq','base_url':'https://wrong.example'})
        self.assertEqual(row['base_url'], 'https://api.groq.com/openai/v1')
        self.assertFalse(provider_presets()['inference'])
        with self.assertRaises(APIModelError): normalize_provider({'provider':'unknown'})

    def test_catalog_projection_is_bounded_and_does_not_claim_allowance(self):
        result = project_models({'data':[{'id':'team/new','name':'New model','supported_parameters':['tools']}, {'id':'old','active':False}, {'id':'../bad'}, None], 'has_more':True})
        self.assertEqual([r['id'] for r in result['models']], ['team/new'])
        self.assertTrue(result['models'][0]['tool_support']); self.assertIsNone(result['models'][0]['quota'])
        self.assertTrue(result['truncated'])
        with self.assertRaises(APIModelError): project_models({'models':[]})

    def test_get_only_with_correct_headers_and_no_redirects_or_raw_error(self):
        sent=[]
        def handler(req):
            sent.append(req); return httpx.Response(200,json={'data':[{'id':'model-one'}]})
        with patch('app.model_discovery.check_public_host'), patch('app.model_discovery.httpx.Client',side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(handler),**kw)):
            self.assertEqual(fetch_models('https://api.example/v1','openai','secret-value')['models'][0]['id'],'model-one')
            fetch_models('https://api.example/v1','anthropic','secret-value')
        self.assertEqual([r.method for r in sent],['GET','GET'])
        self.assertEqual(str(sent[0].url),'https://api.example/v1/models')
        self.assertEqual(sent[0].headers['authorization'],'Bearer secret-value')
        self.assertEqual(sent[1].headers['x-api-key'],'secret-value')
        self.assertEqual(sent[1].url.params['limit'],'1000')
        sent.clear()
        def redirect(req):
            sent.append(req); return httpx.Response(302,headers={'location':'https://elsewhere.example/models'},text='secret-value')
        with patch('app.model_discovery.check_public_host'), patch('app.model_discovery.httpx.Client',side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(redirect),**kw)):
            with self.assertRaises(APIModelError) as error: fetch_models('https://api.example/v1','openai','secret-value')
        self.assertEqual(len(sent),1); self.assertNotIn('secret-value',str(error.exception))

    def test_public_catalog_without_key_and_no_issuer_guess(self):
        with patch('app.model_discovery.fetch_models',return_value={'models':[]}) as fetch:
            result=discover(self.store,{'provider':'openrouter'})
            self.assertFalse(result['inference']); self.assertEqual(fetch.call_args.args[2],'')
            for body in [{'provider':'groq'}, {'provider':''}]:
                with self.assertRaises(APIModelError): discover(self.store,body)
            self.assertEqual(fetch.call_count,1)

    def test_saved_key_cannot_be_sent_to_a_changed_endpoint(self):
        row=self.add()
        body={'provider':'custom','base_url':self.body['base_url'],'protocol':'openai','connection_id':row['id']}
        with patch('app.model_discovery.fetch_models',return_value={'models':[]}) as fetch:
            result=discover(self.store,body)
            self.assertEqual(fetch.call_args.args[2],self.body['api_key'])
            self.assertNotIn(self.body['api_key'],json.dumps(result))
            for changed in [{'base_url':'https://other.example/v1'}, {'protocol':'anthropic'}]:
                with self.assertRaises(APIModelError): discover(self.store,{**body,**changed})
            self.assertEqual(fetch.call_count,1)

del APIModelsTests
