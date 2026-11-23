"""RemoteOK, HN Who's Hiring, The Muse, Findwork, Careerjet, Workable and SmartRecruiters parsing and filters."""
from jobhunter.sources import careerjet, findwork, hnhiring, remoteok, themuse, watchlist

CFG = {"search": {"queries": ["nurse"], "title_any": ["nurse", "sysadmin"], "max_age_days": 3650},
       "sources": {k: {"enabled": True, "min_hours_between": 0} for k in
                   ("remoteok", "hnhiring", "themuse", "findwork", "careerjet", "watchlist")}}


def test_remoteok(monkeypatch):
    feed = [{"legal": "notice"},
            {"id": "1", "position": "ICU Nurse", "company": "A", "location": "Europe", "date": "2026-11-20T00:00:00+00:00",
             "description": "<p>hi</p>", "url": "https://remoteok.com/1"},
            {"id": "2", "position": "Chef", "company": "B", "location": "Europe", "date": "2026-11-20T00:00:00+00:00"},
            {"id": "3", "position": "Nurse", "company": "C", "location": "USA only", "date": "2026-11-20T00:00:00+00:00"}]
    monkeypatch.setattr(remoteok, "get_json", lambda *a, **k: feed)
    jobs = list(remoteok.fetch(CFG))
    assert [j["external_id"] for j in jobs] == ["1"] and jobs[0]["remote"] and jobs[0]["description"] == "hi"


def test_hn_post_parsing():
    j = hnhiring.parse_post({"id": 9, "created_at": "2026-11-02T10:00:00Z",
                             "text": "Acme | Senior Sysadmin | Barcelona, Spain | REMOTE (EU)<p>We run servers."})
    assert j["company"] == "Acme" and j["title"] == "Senior Sysadmin" and j["remote"]
    assert j["url"].endswith("id=9") and "Barcelona" in j["location"]
    assert hnhiring.parse_post({"id": 1, "text": "just a comment"}) is None


def test_themuse(monkeypatch):
    d = {"page_count": 1, "results": [
        {"id": 5, "name": "Registered Nurse", "company": {"name": "H"}, "locations": [{"name": "Flexible / Remote"}],
         "refs": {"landing_page": "https://themuse.com/5"}, "contents": "<b>x</b>", "publication_date": "2026-11-21T00:00:00Z"},
        {"id": 6, "name": "Nurse", "company": {"name": "H"}, "locations": [{"name": "Chicago, IL"}],
         "publication_date": "2026-11-21T00:00:00Z"}]}
    seen = []
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return d
    monkeypatch.setattr(themuse.requests, "get", lambda url, params=None, **k: seen.append(params) or R())
    jobs = list(themuse.fetch(CFG))
    assert [j["external_id"] for j in jobs] == ["5"] and "Flexible / Remote" in seen[0]["location"]


def test_findwork_needs_key_and_parses(monkeypatch):
    import pytest
    monkeypatch.setattr(findwork, "env", lambda n: None)
    with pytest.raises(RuntimeError):
        list(findwork.fetch(CFG))
    monkeypatch.setattr(findwork, "env", lambda n: "k")
    hdrs = {}
    monkeypatch.setattr(findwork, "get_json", lambda url, params=None, headers=None: hdrs.update(headers) or {"results": [
        {"id": 1, "role": "Sysadmin", "company_name": "Z", "remote": True, "url": "u", "date_posted": "2026-11-20T00:00:00Z"}]})
    cfg = {**CFG, "search": {**CFG["search"], "queries": ["sysadmin"]}}
    assert [j["title"] for j in findwork.fetch(cfg)] == ["Sysadmin"] and hdrs["Authorization"] == "Token k"


def test_careerjet(monkeypatch):
    monkeypatch.setattr(careerjet, "env", lambda n: "key")
    monkeypatch.setattr(careerjet, "_ip", lambda: "1.2.3.4")
    calls = []

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"type": "JOBS", "jobs": [{"title": "Enfermera", "company": "Q", "locations": "Barcelona",
                                              "date": "Fri, 20 Nov 2026 10:00:00 GMT", "url": "https://jvt/1",
                                              "description": "turno <b>remoto</b>"}]}
    monkeypatch.setattr(careerjet.requests, "get", lambda url, params=None, auth=None, **k: calls.append((params, auth)) or R())
    cfg = {**CFG, "sources": {**CFG["sources"], "careerjet": {"enabled": True, "min_hours_between": 0, "locations": ["Barcelona"]}}}
    jobs = list(careerjet.fetch(cfg))
    assert jobs[0]["title"] == "Enfermera" and jobs[0]["remote"] and jobs[0]["posted_at"].startswith("2026-11-20")
    assert calls[0][1] == ("key", "") and calls[0][0]["user_ip"] == "1.2.3.4" and calls[0][0]["locale_code"] == "es_ES"


def test_workable_and_smartrecruiters(monkeypatch):
    def gj(url, params=None, **k):
        if "workable" in url:
            return {"name": "W", "jobs": [{"title": "Nurse", "shortcode": "AB", "city": "Barcelona", "country": "Spain",
                                           "url": "https://apply.workable.com/w/j/AB", "published_on": "2026-11-20"}]}
        return {"totalFound": 1, "content": [{"id": "77", "name": "Sysadmin", "company": {"name": "S"},
                                              "location": {"city": "Madrid", "country": "es", "remote": True},
                                              "releasedDate": "2026-11-20T00:00:00Z"}]}
    monkeypatch.setattr(watchlist, "get_json", gj)
    cfg = {**CFG, "sources": {"watchlist": {"enabled": True, "companies": [
        {"name": "W", "ats": "workable", "board": "w"}, {"name": "S", "ats": "smartrecruiters", "board": "S1"}]}}}
    jobs = list(watchlist.fetch(cfg))
    assert {j["external_id"] for j in jobs} == {"wk-w-AB", "sr-S1-77"}
    assert any(j["url"] == "https://jobs.smartrecruiters.com/S1/77" for j in jobs)
