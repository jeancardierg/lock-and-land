"""Word lists the validator uses. Plain data, no logic beyond compiling regexes.

TOOLS: product and skill names. Any of these in a letter must be backed by a cited fact (or, for the
"global" check, by the profile at all). Aliases map to one canonical name.
VERBS: ownership verbs by level, English and Spanish. A sentence may not use a verb above the level of
the facts it cites.
"""
from __future__ import annotations

import re
import unicodedata

# canonical name -> extra spellings (the canonical name itself always matches)
TOOLS: dict[str, list[str]] = {
    # endpoint / MDM
    "Microsoft Intune": ["intune", "endpoint manager", "mem"],
    "Windows Autopilot": ["autopilot"],
    "Windows LAPS": ["laps"],
    "Endpoint Privilege Management": ["epm"],
    "SCCM": ["mecm", "configmgr", "configuration manager", "system center"],
    "Jamf": ["jamf pro", "jamf school"],
    "Iru": ["kandji"],
    "Apple Business Manager": ["abm", "apple business essentials"],
    "Workspace ONE": ["airwatch", "vmware workspace one"],
    "Mosyle": [], "Addigy": [], "Hexnode": [], "SAP Privileges": [],
    "Settings Catalog": ["csp"],
    "Group Policy": ["gpo", "gpos", "group policies"],
    # identity
    "Okta": ["okta verify", "fastpass", "okta workflows"],
    "Entra ID": ["azure ad", "azure active directory", "aad", "entra"],
    "Active Directory": ["ad ds", "on-prem ad", "domain controller", "domain controllers"],
    "SAML": [], "SCIM": [], "SSO": ["single sign-on"], "MFA": ["2fa", "multi-factor"],
    "OneLogin": [], "Ping Identity": ["pingfederate", "pingone"], "CyberArk": [], "SailPoint": [],
    "Keycloak": [], "JumpCloud": [], "Auth0": [],
    # security
    "Bitdefender GravityZone": ["bitdefender", "gravityzone"],
    "CrowdStrike": ["falcon"], "SentinelOne": [], "Sophos": [], "Wazuh": [],
    "Microsoft Defender": ["defender for endpoint", "windows defender", "mde"],
    "Splunk": [], "Microsoft Sentinel": ["azure sentinel"], "Qualys": [], "Tenable": ["nessus"],
    "ISO 27001": ["iso27001", "iso/iec 27001"], "SOC 2": ["soc2"], "NIS2": [], "GDPR": ["rgpd"],
    "EDR": [], "SIEM": [], "XDR": [], "DLP": [], "CIS benchmarks": ["cis controls"],
    # SaaS / ITSM / ITAM
    "Google Workspace": ["g suite", "gsuite"],
    "Google Apps Script": ["apps script"],
    "Microsoft 365": ["office 365", "o365", "m365"],
    "Exchange Online": ["exchange"], "SharePoint": [], "Teams": ["microsoft teams"],
    "Jira Service Management": ["jsm", "jira service desk"],
    "Jira": [], "Confluence": [], "Slack": [], "Atlassian Rovo": ["rovo"],
    "ServiceNow": [], "Zendesk": [], "Freshservice": ["freshdesk"], "TOPdesk": [],
    "AssetSonar": [], "Snipe-IT": [], "Lansweeper": [],
    "Factorial": [], "Leapsome": [], "Workday": [], "BambooHR": [], "Personio": [],
    "Claude": ["claude team"], "ChatGPT": ["openai"], "Copilot": ["microsoft copilot"],
    # scripting / platforms / infra
    "PowerShell": [], "zsh": [], "Bash": ["shell scripting"], "Python": [],
    "Terraform": [], "Ansible": [], "Puppet": [], "Chef": [], "Kubernetes": ["k8s"], "Docker": [],
    "AWS": ["amazon web services"], "Azure": ["microsoft azure"], "GCP": ["google cloud"],
    "VMware": ["vsphere", "esxi"], "Hyper-V": [], "Citrix": [], "AVD": ["azure virtual desktop"],
    "Linux": ["ubuntu", "red hat", "rhel", "debian"], "macOS": [], "Windows Server": [],
    "Veritas": ["backup exec", "netbackup"], "Veeam": [],
    "Tailscale": [], "VLAN": ["vlans"], "Cisco": [], "Meraki": [], "Fortinet": ["fortigate"],
    "Ubiquiti": ["unifi"], "Palo Alto": [], "Zscaler": [],
    # engineering / materials / manufacturing / lab tools (so letters can't claim ones the CV doesn't show)
    "SolidWorks": [], "AutoCAD": [], "CATIA": [], "Siemens NX": [], "Creo": [], "Inventor": [], "Fusion 360": [],
    "ANSYS": [], "Abaqus": [], "COMSOL": [], "MATLAB": [], "Simulink": [], "LabVIEW": [], "Minitab": [],
    "JMP": [], "Origin": ["originlab"], "Thermo-Calc": ["thermocalc"], "SEM": ["scanning electron microscopy"],
    "TEM": ["transmission electron microscopy"], "XRD": ["x-ray diffraction"], "XPS": [], "EDS": ["edx"],
    "FTIR": ["ft-ir"], "Raman": ["raman spectroscopy"], "DSC": [], "TGA": [], "DMA": [], "AFM": [],
    "nanoindentation": [], "metallography": [], "rheology": ["rheometry"], "tensile testing": [],
    "injection moulding": ["injection molding"], "additive manufacturing": ["3d printing"], "CNC": [],
    "FMEA": [], "SPC": [], "DOE": ["design of experiments"], "PPAP": [], "APQP": [], "8D": [], "root cause analysis": [],
    "ISO 9001": [], "IATF 16949": [], "ISO 13485": [], "GMP": [], "REACH": [], "RoHS": [], "SAP": [],
    # methods / certifications people like to claim
    "ITIL": [], "Agile": ["agile methodologies", "agile methodology"], "Scrum": [], "Kanban": [],
    "DevOps": [], "Prince2": [], "PMP": [], "Six Sigma": [], "Lean": [],
}

# Spanish and English verbs by ownership level. Level ladder (Module 2 of the blueprint):
LEVELS = {"supported": 1, "executed": 2, "co_delivered": 3, "led_delivery": 4, "led_solo": 5, "owned": 6,
          "designed": 7}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}

# (pattern, level, past_only). past_only patterns count in motivation sentences too.
VERBS: list[tuple[str, int]] = [
    # 1 supported
    (r"support(ed|ing)? the", 1), (r"assist(ed|ing)", 1), (r"helped", 1), (r"contributed", 1),
    (r"apoy(é|e|aba|ando)", 1), (r"colabor(é|aba)", 1),
    # 3 co-delivered
    (r"co-?(led|delivered|lead|ran)", 3), (r"partnered", 3), (r"codirig(í|i)", 3),
    # 4 led
    (r"led", 4), (r"leading", 4), (r"lead(s)?", 4), (r"drove", 4), (r"driving", 4), (r"drive[sn]?", 4),
    (r"spearhead(ed|ing)?", 4), (r"headed", 4), (r"direct(ed|ing)", 4), (r"orchestrat(ed|ing)", 4),
    (r"champion(ed|ing)", 4), (r"manag(ed|ing) the (team|project|programme|program|rollout)", 4),
    (r"lider(é|e|ando|o|aba)", 4), (r"dirig(í|i|iendo)", 4), (r"encabec(é|e)", 4), (r"impuls(é|ando)", 4),
    # 5 alone / sole
    (r"single-?handed(ly)?", 5), (r"alone", 5), (r"on my own", 5), (r"sole", 5), (r"solely", 5),
    (r"by myself", 5), (r"en solitario", 5), (r"(ú|u)nico responsable", 5), (r"yo solo", 5),
    # 6 own
    (r"own(ed|s|ing)?", 6), (r"owner(ship)?", 6), (r"accountable", 6), (r"responsible for", 6),
    (r"in charge of", 6), (r"responsable (de|del|actual)", 6), (r"a cargo de", 6),
    # 7 designed / planned (never claimed unless a fact says so)
    (r"design(ed|ing)?", 7), (r"architect(ed|ing)", 7), (r"plann(ed|ing)", 7), (r"conceiv(ed|ing)", 7),
    (r"devised", 7), (r"created the (strategy|roadmap|plan)", 7), (r"set the (strategy|roadmap)", 7),
    (r"dise(ñ|n)(é|e|ando|o)", 7), (r"planifiqu(é|e)", 7), (r"plane(é|e)", 7), (r"ide(é|e)", 7),
]
# Past-tense achievement verbs: forbidden in "motivation" sentences (those may not claim results).
PAST_RESULT = re.compile(
    r"\b(led|drove|owned|designed|architected|planned|delivered|built|deployed|rolled out|implemented|migrated|"
    r"automated|reduced|increased|achieved|resolved|launched|spearheaded|managed|ran|wrote|cut|saved|halved|"
    r"lider[ée]|dise[ñn][ée]|despleg[uée]+|implement[ée]|migr[ée]|automatic[ée]|reduje|consegu[íi]|logr[ée])\b", re.I)

BANNED = [r"\bI am (so )?excited\b", r"\bI am writing to\b", r"\bextensive experience\b", r"\btrack record\b",
          r"\bexceptional\b", r"\bpassionate\b", r"\bthrilled\b", r"\bproven\b", r"\bworld-class\b",
          r"\bseasoned\b", r"\bexpert in\b", r"\bguru\b", r"\brockstar\b", r"\bsynerg", r"\bdynamic professional\b",
          r"\bamplia experiencia\b", r"\bme apasiona\b", r"\bapasionad[oa]\b", r"\bexperto en\b"]

CERT = re.compile(r"\b(certified|certification|certificate|certificaci[óo]n|certificad[oa]|CISSP|CISM|CCNA|CCNP|"
                  r"MD-?102|MS-?102|AZ-?\d{3}|SC-?\d{3}|Security\+|Network\+|A\+|ITIL (v\d|foundation|4))\b", re.I)
SALARY = re.compile(r"(€|eur\b|euros?\b|\$|£|k ?(€|eur)|salary of|salario de)\s*\d|\d[\d.,]*\s*(k|€|eur|euros)\b",
                    re.I)
FALSE_TITLE = re.compile(
    r"\b(my (current )?(role|position|job|title) as|in my (current )?role as|currently (work(ing)? )?as|i am (an? |the )?|"
    r"i'm (an? |the )?|mi (puesto|rol|cargo) (actual )?(de|como)|actualmente (trabajo )?como|soy)\s*"
    r"(an? |el |la )?(senior )?(it (manager|lead|director|head)|head of it|systems? administrator|sysadmin|"
    r"administrador de sistemas|responsable de it|jefe de it|director de it|endpoint engineer|identity engineer)\b",
    re.I)
_TITLE_AT = (r"\b(as|como)\s+(an? |the |el |la )?(senior )?(it (manager|lead|director|head)|head of it|systems? administrator|"
             r"sysadmin|administrador de sistemas|responsable de it|jefe de it|director de it)\s+(at|in|for|with|en|de)\s+")


def false_title_at(employers) -> re.Pattern | None:
    """'as IT Manager at <one of the candidate's own employers>': a title the profile doesn't hold, at a real employer."""
    names = sorted({fold(e).strip() for e in employers if e and fold(e).strip()}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(_TITLE_AT + "(" + "|".join(re.escape(n) for n in names) + ")", re.I)


CAREER_BREAK = re.compile(r"\b(career break|gap|sabbatical|time off|pausa profesional|par[ée]ntesis)\b", re.I)


def fold(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", t or "") if not unicodedata.combining(c)).lower()


def _alias_re(name: str) -> str:
    esc = re.escape(fold(name)).replace(r"\ ", r"[\s-]+")
    return rf"(?<![a-z0-9]){esc}(?![a-z0-9])"


_TOOL_RES: list[tuple[str, re.Pattern]] = []
for _canon, _aliases in TOOLS.items():
    for _name in [_canon, *_aliases]:
        _TOOL_RES.append((_canon, re.compile(_alias_re(_name))))
# longest names first so "Jira Service Management" wins over "Jira"
_TOOL_RES.sort(key=lambda x: -len(x[1].pattern))
_CASE_SENSITIVE = {"MDM", "AD", "IT", "EDR", "SSO", "MFA", "SCIM", "SAML", "EPM", "ABM", "MEM", "CSP", "JSM", "AAD"}


def find_tools(text: str) -> list[str]:
    """Canonical tool names mentioned in text. Short all-caps aliases must match in capitals."""
    t = fold(text)
    found: dict[str, None] = {}
    spans: list[tuple[int, int]] = []
    for canon, rx in _TOOL_RES:
        for m in rx.finditer(t):
            if any(a <= m.start() < b for a, b in spans):
                continue  # inside a longer match already counted
            raw = text[m.start():m.end()] if len(text) == len(t) else m.group(0)
            if m.group(0).upper() in _CASE_SENSITIVE and raw != raw.upper():
                continue
            spans.append((m.start(), m.end()))
            found[canon] = None
    return list(found)


def strip_tools(text: str) -> str:
    """Remove tool names (so 'Microsoft 365' or 'ISO 27001' don't count as claimed numbers)."""
    t = fold(text)
    for _, rx in _TOOL_RES:
        t = rx.sub(" ", t)
    return t


_VERB_RES = [(re.compile(rf"(?<![a-z]){p}(?![a-z])", re.I), lvl) for p, lvl in VERBS]


def verb_matches(text: str) -> list[tuple[str, int]]:
    """Every ownership verb in text with its level."""
    low = text.lower()
    out = []
    for rx, lvl in _VERB_RES:
        for m in rx.finditer(low):
            out.append((m.group(0), lvl))
    return out


def verb_level(text: str) -> tuple[int, str]:
    """Highest ownership level claimed by verbs in text: (level, matched words). 0 if none."""
    best, word = 0, ""
    low = text.lower()
    for rx, lvl in _VERB_RES:
        m = rx.search(low)
        if m and lvl > best:
            best, word = lvl, m.group(0)
    return best, word


_NUM = re.compile(r"(?<![a-z0-9])[~≈+]?\d[\d.,]*(?:\s?[–-]\s?\d[\d.,]*)?\s?%?\+?", re.I)
NUMBER_WORDS = {"two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                "nine": "9", "ten": "10", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
                "siete": "7", "ocho": "8", "nueve": "9", "diez": "10"}


def canon_number(tok: str) -> str:
    t = tok.strip().strip("~≈+%").strip()
    t = t.rstrip(".,")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", t):
        return re.sub(r"[.,]", "", t)  # 1,800 / 1.800 -> 1800
    return t.replace(",", ".")


def numbers(text: str) -> set[str]:
    """Canonical numbers in text, ignoring numbers that are part of tool names. Ranges give both ends."""
    t = strip_tools(text)
    out: set[str] = set()
    for m in _NUM.finditer(t):
        for part in re.split(r"\s?[–-]\s?", m.group(0)):
            c = canon_number(part)
            if c and re.search(r"\d", c):
                out.add(c)
    for w in re.findall(r"[a-z]+", t):
        if w in NUMBER_WORDS:
            out.add(NUMBER_WORDS[w])
    return out
