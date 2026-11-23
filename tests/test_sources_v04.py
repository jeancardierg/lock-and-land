"""v0.4 sources: parsing and filters (no network)."""
from jobhunter.sources import base, himalayas, jobicy, weworkremotely, watchlist, jooble, with_defaults

CFG = {"search": {"queries": ["intune"], "max_age_days": 14, "title_any": base.DEFAULT_TITLE_ANY}}


def test_title_and_location_gates():
    assert base.title_ok("Senior Systems Administrator", CFG)
    assert base.title_ok("Ingeniero de sistemas (Intune)", CFG)
    assert not base.title_ok("Senior React Developer", CFG)
    assert not base.title_ok("Liaison Officer", CFG)  # 'iam' inside a word doesn't count
    assert base.location_ok("") and base.location_ok("Europe") and base.location_ok("Spain, Portugal")
    assert not base.location_ok("USA") and not base.location_ok("United States, Canada")


def test_himalayas_parse():
    j = himalayas.parse_job({"guid": "g1", "title": "IT Manager", "companyName": "Acme", "pubDate": 1795300000,
                             "locationRestrictions": [{"name": "Spain"}], "applicationLink": "https://h/x",
                             "description": "<p>Run IT</p>"})
    assert j["location"] == "Remote (Spain)" and j["url"] == "https://h/x" and j["posted_at"].startswith("2026")
    assert himalayas.parse_job({"guid": "g2", "locationRestrictions": ["Spain", "Portugal"]})["location"] == \
        "Remote (Spain, Portugal)"


def test_jobicy_parse():
    j = jobicy.parse_job({"id": 5, "jobTitle": "Endpoint Engineer", "companyName": "B", "jobGeo": "Europe",
                          "url": "https://jobicy.com/x", "pubDate": "2026-11-20T10:00:00+00:00"})
    assert j["external_id"] == "5" and "Europe" in j["location"]


def test_wwr_feed():
    xml = b"""<rss><channel><item><title>Acme: Senior Sysadmin</title><region>Europe Only</region>
    <link>https://weworkremotely.com/x</link><guid>g</guid><pubDate>Fri, 20 Nov 2026 10:00:00 +0000</pubDate>
    <description>&lt;p&gt;hi&lt;/p&gt;</description></item></channel></rss>"""
    (job, region), = weworkremotely.parse_feed(xml)
    assert job["company"] == "Acme" and job["title"] == "Senior Sysadmin" and region == "Europe Only"


def test_jooble_parse():
    j = jooble.parse_job({"id": 1, "title": "<b>Técnico de Sistemas</b>", "location": "Barcelona",
                          "snippet": "Modalidad híbrida", "link": "https://es.jooble.org/x", "updated": "2026-11-20T12:55:35.387"})
    assert j["title"] == "Técnico de Sistemas" and j["remote"] and j["posted_at"] == "2026-11-20T12:55:35"


def test_watchlist_filters(monkeypatch):
    data = {"jobs": [
        {"id": 1, "title": "IT Operations Engineer", "location": {"name": "Barcelona"}, "absolute_url": "u1",
         "content": "&lt;p&gt;x&lt;/p&gt;", "updated_at": "2026-11-20T10:00:00Z"},
        {"id": 2, "title": "IT Manager", "location": {"name": "New York (US only)"}, "absolute_url": "u2",
         "updated_at": "2026-11-20T10:00:00Z"},
        {"id": 3, "title": "Backend Developer", "location": {"name": "Remote"}, "absolute_url": "u3",
         "updated_at": "2026-11-20T10:00:00Z"}]}
    monkeypatch.setattr(watchlist, "get_json", lambda url, **kw: data)
    cfg = {**CFG, "sources": {"watchlist": {"companies": [{"name": "Acme", "ats": "greenhouse", "board": "acme"}]}}}
    out = list(watchlist.fetch(cfg))
    assert [j["external_id"] for j in out] == ["gh-acme-1"] and out[0]["company"] == "Acme"


def test_new_free_sources_default_on():
    cfg = with_defaults({"sources": {"remotive": {"enabled": True}, "jobicy": {"enabled": False}}})
    assert cfg["sources"]["himalayas"]["enabled"] and not cfg["sources"]["jobicy"]["enabled"]


def test_title_gate_follows_your_roles():
    cfg = {"search": {"queries": ["nurse", "care assistant"]}}
    assert base.title_ok("Registered Nurse (ICU)", cfg) and not base.title_ok("Systems Administrator", cfg)
