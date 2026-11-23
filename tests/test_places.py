"""Ads shorten and translate town names; your commute area should still recognise them."""
import json

from jobhunter import geo, places, rules

CFG = {"rules": {"home_cities": ["barcelona", "l'hospitalet de llobregat", "sant cugat del vallès", "girona",
                                 "esplugues de llobregat", "santa coloma de gramenet", "el prat de llobregat"]}}


def test_short_and_other_language_forms_are_local():
    for ad in ["Hospitalet", "L'Hospitalet", "Hospitalet de Llobregat", "Sant Cugat", "San Cugat del Vallés",
               "St. Cugat", "Gerona", "Esplugues", "El Prat", "Santa Coloma de Gramanet", "BCN", "L-Hospitalet"]:
        assert rules.is_local(ad, CFG), ad


def test_different_places_are_not():
    for ad in ["Santa Coloma de Farners", "Madrid", "Sant Joan de Vilatorrada", "Cugat", "Coloma"]:
        assert not rules.is_local(ad, CFG), ad


def test_ambiguous_short_forms_are_never_derived():
    v = places.variants("Santa Coloma de Gramenet")
    assert "santa coloma" not in v and "sta coloma" not in v
    assert "san sebastian" not in places.variants("San Sebastián de los Reyes")
    assert "san sebastian" in places.variants("Donostia")  # a known other name is fine


def test_map_aliases_from_openstreetmap(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_home", lambda: tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "town_aliases.json").write_text(json.dumps({"vilafranca del penedes": ["Villafranca del Panadés"]}))
    cfg = {"rules": {"home_cities": ["Vilafranca del Penedès"]}}
    assert rules.is_local("Villafranca del Panadés", cfg) and rules.is_local("Vilafranca", cfg)
