"""Offline acceptance of cached content, budget sharing and secret-safe provenance."""
import json
from pathlib import Path

import httpx
import pytest

from harvester.evidence import build_evidence_export
from harvester.models import ArtifactKind, RunStatus
from harvester.providers.openalex import content_candidates, is_content_url
from mocks import Behavior, MockProviders, make_pdf_bytes, openalex_work

PDF = 'https://content.openalex.org/works/W2000001.pdf'
XML = 'https://content.openalex.org/works/W2000001.grobid-xml'
TEI = b'<?xml version="1.0"?><TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><p>Mock full text from GROBID.</p></body></text></TEI>'

class ContentProviders(MockProviders):
    def __init__(self, *, publisher_ok=False, content_status=200, content_body=None):
        work = openalex_work(1)
        work['content_urls'] = {'pdf': PDF, 'grobid_xml': XML}
        super().__init__(works=[work], files={'/W2000001.pdf': make_pdf_bytes()} if publisher_ok else {})
        self.content_status = content_status
        self.content_body = content_body
        if not publisher_ok:
            self.script_route('file:/W2000001.pdf', [Behavior(status=403)])

    def _route_for(self, request):
        if request.url.host == 'content.openalex.org':
            return 'content:' + request.url.path
        return super()._route_for(request)

    def _default(self, route, request):
        if route.startswith('content:'):
            body = self.content_body
            if body is None:
                body = TEI if request.url.path.endswith('grobid-xml') else make_pdf_bytes()
            return httpx.Response(self.content_status, content=body, request=request)
        return super()._default(route, request)


def test_publisher_403_then_cached_pdf_and_tei_with_no_secret_leak(config, store, harvester_factory, query, caplog):
    config.europe_pmc.enabled = False
    providers = ContentProviders()
    harvester = harvester_factory(providers)
    result = harvester.harvest(query)
    assert result.status is RunStatus.COMPLETED
    requests = providers.requests
    content = [r for r in requests if r.url.host == 'content.openalex.org']
    assert len(content) == 2
    assert all(r.url.params['api_key'] == config.openalex.api_key for r in content)
    assert all('api_key' not in r.url.params for r in requests if r.url.host == 'files.invalid')
    assert harvester.clients.openalex.budget.used_by_this_process == 210
    ledger = store.provider_requests_for_run(result.run_id)
    pdf_rows = [r for r in ledger if r['operation'] == 'acquire.pdf']
    assert [r['outcome']['status'] for r in pdf_rows] == ['ERROR', 'HIT']
    assert pdf_rows[-1]['request']['request_url'] == PDF + '?api_key=REDACTED'
    discovery = ledger[0]['request']['request_url']
    assert 'select=' in discovery and 'cursor=' in discovery and 'api_key=REDACTED' in discovery
    export = json.dumps(build_evidence_export(store))
    assert config.openalex.api_key not in export
    assert config.openalex.api_key not in caplog.text
    for root in (Path(config.storage_root), Path(config.reports_dir), Path(config.state_db).parent):
        for path in root.rglob('*'):
            if path.is_file():
                assert config.openalex.api_key.encode() not in path.read_bytes(), path
    assert 'TEI' in next(Path(config.storage_root).glob('*.xml')).read_text()


def test_successful_publisher_avoids_paid_pdf(config, store, harvester_factory, query):
    config.xml_policy = 'disabled'
    config.europe_pmc.enabled = False
    providers = ContentProviders(publisher_ok=True)
    harvester_factory(providers).harvest(query)
    assert not any(r.url.host == 'content.openalex.org' for r in providers.requests)


def test_no_key_does_not_offer_or_attempt_content(config, harvester_factory, query):
    config.openalex.api_key = None
    config.openalex.allow_keyless = True
    config.europe_pmc.enabled = False
    providers = ContentProviders()
    harvester_factory(providers).harvest(query)
    assert not any(r.url.host == 'content.openalex.org' for r in providers.requests)


def test_content_shares_budget_ceiling(config, harvester_factory, query):
    config.europe_pmc.enabled = False
    config.openalex.daily_credit_ceiling = 110
    providers = ContentProviders()
    harvester = harvester_factory(providers)
    result = harvester.harvest(query)
    assert result.status is RunStatus.SUSPENDED
    assert harvester.clients.openalex.budget.used_by_this_process == 110
    assert len([r for r in providers.requests if r.url.host == 'content.openalex.org']) == 1


@pytest.mark.parametrize('status,body', [(401,b'Unauthorized'),(404,b'Missing'),(200,b'<html>Blocked</html>')])
def test_invalid_content_never_publishes_artifacts(config, harvester_factory, query, status, body):
    config.europe_pmc.enabled = False
    config.xml_policy = 'disabled'
    providers = ContentProviders(content_status=status, content_body=body)
    harvester_factory(providers).harvest(query)
    assert not list(Path(config.storage_root).glob('*.pdf'))
    assert not list(Path(config.storage_root).glob('*.part'))


@pytest.mark.parametrize('url', [
    'http://content.openalex.org/works/W2000001.pdf',
    'https://content.openalex.org.evil.invalid/works/W2000001.pdf',
    'https://user@content.openalex.org/works/W2000001.pdf',
    PDF + '?api_key=evil', PDF + '#fragment',
    'https://content.openalex.org/works/W999.pdf',
])
def test_untrusted_content_urls_cannot_receive_key(url):
    assert content_candidates({'content_urls': {'pdf': url}}, 'W2000001') == []


def test_absent_or_malformed_content_is_backward_compatible():
    for value in (None, [], 'wrong', {}):
        assert content_candidates({'content_urls': value}, 'W2000001') == []
    assert [c.kind for c in content_candidates({'content_urls': {'pdf': PDF, 'grobid_xml': XML}}, 'W2000001')] == [ArtifactKind.PDF, ArtifactKind.XML]


def test_authenticated_error_body_cannot_leak_key(config, store, harvester_factory, query, caplog):
    config.europe_pmc.enabled = False
    config.xml_policy = 'disabled'
    providers = ContentProviders(content_status=500, content_body=('echo api_key=' + config.openalex.api_key).encode())
    harvester_factory(providers).harvest(query)
    assert config.openalex.api_key not in json.dumps(build_evidence_export(store))
    assert config.openalex.api_key not in caplog.text


def test_content_network_error_cannot_leak_authenticated_url(config, store, harvester_factory, query, caplog):
    config.europe_pmc.enabled = False
    config.xml_policy = 'disabled'
    providers = ContentProviders()
    providers.script_route('content:/works/W2000001.pdf', [
        Behavior(raise_exc=lambda: httpx.ConnectError('failed ' + PDF + '?api_key=' + config.openalex.api_key))
        for _ in range(config.retry.max_attempts)
    ])
    harvester_factory(providers).harvest(query)
    assert config.openalex.api_key not in json.dumps(build_evidence_export(store))
    assert config.openalex.api_key not in caplog.text
