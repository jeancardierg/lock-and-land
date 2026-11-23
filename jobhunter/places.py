"""Town names as job ads actually write them.

Your commuting towns are saved under their official names ("L'Hospitalet de Llobregat", "Sant Cugat del Vallès"),
but ads shorten and translate them: "Hospitalet", "Sant Cugat", "San Cugat del Vallés", "St. Cugat", "Gerona".
variants() gives every form we should accept for a town, and local_re() one regex for a whole list.

Forms come from: the name without accents and punctuation; without a leading article (l', el, la, els, les...);
without the "de/del/d' ..." part that says which comarca or river it's on; Sant <-> San <-> St.; well-known
Catalan/Spanish/Basque/Galician pairs; and the other-language names OpenStreetMap gave when the town was added from
the map. A short form that is itself a different well-known place is never derived on its own: "Santa Coloma de
Gramenet" doesn't become "Santa Coloma" (there are several). Add such a short form as its own town if you want it."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

ARTICLES = re.compile(r"^(?:l|el|la|els|les|los|las|lo|o|a|os|as)\s+")
# "de X", "del X", "d'X", "de la X", "dels X", "de les X", "de los X", "de las X" at the end of a name
QUALIFIER = re.compile(r"\s+(?:de|del|d|dels|de la|de les|de los|de las|do|da|dos|das)\s+.+$")
SAINT = [("sant", "san"), ("sant", "st"), ("santa", "sta"), ("san", "sant"), ("san", "st")]

# Short forms that are a different well-known place: never derived automatically.
AMBIGUOUS = {"san sebastian", "santiago", "san juan", "santa cruz", "san jose", "san pedro", "san vicente",
             "santa coloma", "san fernando", "san martin", "sant joan", "sant pere", "sant vicenc", "santa maria",
             "san andres", "sant andreu", "san antonio", "sant antoni", "san miguel", "sant miquel", "santa eulalia",
             "santa eularia", "sant julia", "la", "el", "san", "sant", "santa", "puerto", "villa", "vila", "torre",
             "castell", "castillo", "alcala", "aldea", "palma", "sant marti", "san marcos", "san roque",
             "san lorenzo", "sant llorenc", "san carlos", "sant carles", "san isidro", "santa margarida"}

# Official name -> other names ads use (all folded). Catalan/Valencian, Basque, Galician and common short forms.
KNOWN = {
    "barcelona": ["bcn"], "girona": ["gerona"], "lleida": ["lerida"], "tarragona": [],
    "l hospitalet de llobregat": ["hospitalet", "l hospitalet", "hospitalet de llobregat"],
    "sant cugat del valles": ["sant cugat", "san cugat", "san cugat del valles"],
    "santa coloma de gramenet": ["santa coloma de gramanet"],
    "sant adria de besos": ["sant adria", "san adrian de besos", "san adrian"],
    "esplugues de llobregat": ["esplugues"], "cornella de llobregat": ["cornella"],
    "el prat de llobregat": ["el prat", "prat de llobregat"], "sant boi de llobregat": ["sant boi", "san baudilio"],
    "sant joan despi": ["san juan despi"], "sant just desvern": ["san justo desvern"],
    "cerdanyola del valles": ["cerdanyola", "cerdanola"], "montcada i reixac": ["moncada y reixac", "montcada"],
    "sant feliu de llobregat": ["sant feliu", "san feliu de llobregat"], "vilanova i la geltru": ["vilanova"],
    "sant quirze del valles": ["sant quirze"], "mollet del valles": ["mollet"], "rubi": [], "terrassa": ["tarrasa"],
    "sabadell": [], "granollers": [], "mataro": [], "badalona": [], "castelldefels": [], "gava": [],
    "viladecans": [], "martorell": [], "igualada": [], "manresa": [], "vic": [], "reus": [],
    "valencia": [], "alacant": ["alicante"], "alicante": ["alacant"], "castello de la plana":
    ["castellon", "castellon de la plana", "castello"], "elx": ["elche"], "elche": ["elx"],
    "a coruna": ["la coruna", "coruna"], "ourense": ["orense"], "sanxenxo": ["sangenjo"],
    "donostia": ["san sebastian", "donostia san sebastian"], "donostia san sebastian": ["san sebastian", "donostia"],
    "bilbao": ["bilbo"], "vitoria gasteiz": ["vitoria", "gasteiz"], "iruna": ["pamplona"], "pamplona": ["iruna"],
    "palma": ["palma de mallorca"], "palma de mallorca": ["palma"], "madrid": [], "zaragoza": ["saragossa"],
    "sevilla": ["seville"], "malaga": [], "las palmas de gran canaria": ["las palmas"],
    "santa cruz de tenerife": [], "lisboa": ["lisbon"], "porto": ["oporto"],
}


def fold(t: str) -> str:
    """Lower case, no accents, apostrophes/hyphens/dots as spaces: "L'Hospitalet" -> "l hospitalet"."""
    t = "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _saint(v: str) -> str:
    """Sant/San/St./Sta. -> one spelling, so "Sta. Coloma" counts as the ambiguous "Santa Coloma"."""
    return re.sub(r"^(?:sta|santa)\b", "santa", re.sub(r"^(?:st|sant|san)\b", "san", v))


_AMBIG = {_saint(x) for x in AMBIGUOUS}


def _ok(v: str) -> bool:
    return len(v) >= 4 and _saint(v) not in _AMBIG


@lru_cache(maxsize=4096)
def variants(name: str, alt: tuple[str, ...] = ()) -> frozenset[str]:
    base = fold(name)
    if not base:
        return frozenset()
    explicit = {base} | {fold(a) for a in alt if fold(a)} | {fold(x) for x in KNOWN.get(base, [])}
    out = set(explicit)
    for _ in range(2):  # derived forms of derived forms (article off, then qualifier off)
        for v in list(out):
            no_art = ARTICLES.sub("", v)
            if no_art != v and _ok(no_art):
                out.add(no_art)
            short = QUALIFIER.sub("", v)
            if short != v and _ok(short):
                out.add(short)
            for a, b in SAINT:
                if v.startswith(a + " "):
                    s = b + v[len(a):]
                    if _ok(s):
                        out.add(s)
            out.update(fold(x) for x in KNOWN.get(v, []))
    return frozenset(x for x in out if x in explicit or _ok(x))


def local_re(names: list[str], aliases: dict[str, list[str]] | None = None) -> re.Pattern:
    """One regex matching any form of any of these towns in folded text (see fold())."""
    forms: set[str] = set()
    for n in names:
        forms |= variants(str(n), tuple((aliases or {}).get(fold(str(n)), [])))
    forms = sorted(forms, key=len, reverse=True)
    if not forms:
        return re.compile(r"(?!x)x")
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(f) for f in forms) + r")(?![a-z0-9])")
