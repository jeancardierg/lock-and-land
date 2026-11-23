"""How a job is applied for. The app never fills web forms: bot protection (Cloudflare and similar) blocks it.

  email   the ad gives an address: Lock & Land sends the letter and CV from your Gmail after two confirmations
  web     the ad has a link: you apply in your normal browser with the letter and answers Lock & Land copied
  manual  neither: apply however the ad says, then mark it applied
"""
from __future__ import annotations


def route(job) -> str:
    """email | web | manual"""
    if job["apply_email"]:
        return "email"
    if job["apply_url"] or job["url"]:
        return "web"
    return "manual"
