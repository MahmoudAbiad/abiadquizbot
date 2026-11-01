"""Shared version-aware legal consent flow for Telegram users."""
from helpers.supabase_helper import supabase

TERMS_URL = "https://abiad.me/terms-of-service.html"
PRIVACY_URL = "https://abiad.me/privacy-policy.html"
CURRENT_VERSIONS = {"terms": 1, "privacy": 1}
DOCUMENT_LABELS = {
    "terms": "شروط الاستخدام",
    "privacy": "سياسة الخصوصية",
}


async def current_documents():
    settings = await supabase.table("app_settings").select("key,value").in_(
        "key", ["terms_version", "privacy_version"]
    ).execute()
    versions = dict(CURRENT_VERSIONS)
    for row in settings.data or []:
        name = row["key"].removesuffix("_version")
        if name in versions:
            versions[name] = int(row["value"])

    response = await supabase.table("legal_documents").select(
        "document_type,version,title,url"
    ).in_("document_type", ["terms", "privacy"]).execute()
    documents = {}
    for row in response.data or []:
        if int(row["version"]) == versions[row["document_type"]]:
            documents[row["document_type"]] = row
    documents.setdefault("terms", {"title": "شروط الاستخدام", "url": TERMS_URL})
    documents.setdefault("privacy", {"title": "سياسة الخصوصية", "url": PRIVACY_URL})
    return versions, documents


async def has_current_consent(user_id: int) -> bool:
    versions, _ = await current_documents()
    response = await supabase.table("user_consents").select(
        "status,terms_version,privacy_version"
    ).eq("user_id", user_id).limit(1).execute()
    if not response.data:
        return False
    row = response.data[0]
    return (
        row["status"] == "accepted"
        and int(row["terms_version"]) == versions["terms"]
        and int(row["privacy_version"]) == versions["privacy"]
    )


async def mark_prompted(user_id: int) -> None:
    await supabase.rpc("mark_consent_prompted", {"p_user_id": user_id}).execute()


async def save_decision(user_id: int, decision: str):
    versions, _ = await current_documents()
    result = await supabase.rpc("record_consent", {
        "p_user_id": user_id,
        "p_decision": decision,
        "p_terms_version": versions["terms"],
        "p_privacy_version": versions["privacy"],
    }).execute()
    return result.data




async def bump_document_version(document_type: str) -> int:
    if document_type not in DOCUMENT_LABELS:
        raise ValueError("Unsupported legal document type")
    result = await supabase.rpc(
        "bump_legal_document_version",
        {"p_document_type": document_type},
    ).execute()
    value = result.data
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("bump_legal_document_version")
    new_version = int(value)
    # The settings helper has a short local cache; refresh this process immediately
    # so the admin who bumped a version sees the new prompt behavior right away.
    from helpers.settings_helper import get_app_settings
    await get_app_settings(force_refresh=True)
    return new_version
