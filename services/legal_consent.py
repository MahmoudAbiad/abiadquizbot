"""Shared version-aware legal consent flow for Telegram users."""
from aiogram import types

from helpers.supabase_helper import supabase

TERMS_URL = "https://abiad.me/terms-of-service.html"
PRIVACY_URL = "https://abiad.me/privacy-policy.html"
CURRENT_VERSIONS = {"terms": 1, "privacy": 1}


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


async def consent_prompt(is_new_user: bool):
    _, documents = await current_documents()
    terms = documents["terms"]
    privacy = documents["privacy"]
    if is_new_user:
        body = (
            "📜 <b>قبل أن تبدأ</b>\n\n"
            "لاستخدام أبيض، منشئ اختبارات ذكي، يرجى الاطلاع على سياسة الخصوصية "
            "وشروط الاستخدام والموافقة عليهما للمتابعة."
        )
    else:
        body = (
            "📜 <b>لمتابعة استخدام البوت</b>\n\n"
            "يرجى الاطلاع على سياسة الخصوصية وشروط الاستخدام والموافقة عليهما "
            "لمتابعة استخدام أبيض."
        )
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="سياسة الخصوصية", url=privacy["url"]),
         types.InlineKeyboardButton(text="شروط الاستخدام", url=terms["url"])],
        [types.InlineKeyboardButton(
            text="✅ أوافق",
            callback_data=f"legal_consent:accept:{'new' if is_new_user else 'returning'}",
        ),
         types.InlineKeyboardButton(text="❌ لا أوافق", callback_data="legal_consent:decline")],
    ])
    return body, keyboard
