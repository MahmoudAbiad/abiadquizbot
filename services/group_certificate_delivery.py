"""Private delivery tokens and sending helpers for group quiz certificates."""

from __future__ import annotations

import asyncio
import html
import json
import secrets
from typing import Any, Dict, Optional

from aiogram import Bot, types

from config import redis_client
from services.certificate_service import build_certificate


CERTIFICATE_TTL_SECONDS = 7 * 24 * 60 * 60


def _key(token: str) -> str:
    return f"gq:certificate:{token}"


async def create_certificate_claim(
    *,
    session_id: str,
    user_id: int,
    rank: int,
    score: int,
    total_questions: int,
    quiz_title: str,
) -> str:
    token = secrets.token_urlsafe(18)
    payload = {
        "session_id": session_id,
        "user_id": int(user_id),
        "rank": int(rank),
        "score": int(score),
        "total_questions": int(total_questions),
        "quiz_title": str(quiz_title or "كويز جماعي")[:160],
    }
    await redis_client.set(_key(token), json.dumps(payload, ensure_ascii=False), ex=CERTIFICATE_TTL_SECONDS)
    return token


async def get_certificate_claim(token: str, user_id: int) -> Optional[Dict[str, Any]]:
    if not token or len(token) > 64:
        return None
    raw = await redis_client.get(_key(token))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    if int(payload.get("user_id", 0)) != int(user_id):
        return None
    return payload


async def send_certificate(bot: Bot, chat_id: int, claim: Dict[str, Any], name: str) -> None:
    png, pdf, filename = await asyncio.to_thread(
        build_certificate,
        name=name,
        rank=claim["rank"],
        score=claim["score"],
        total_questions=claim["total_questions"],
        quiz_title=claim["quiz_title"],
    )
    safe_name = html.escape(str(name or "طالب").strip()[:120])
    caption = (
        f"🎓 <b>شهادتك الرمزية من بوت أبيض</b>\n"
        f"الاسم: <b>{safe_name}</b>\n"
        f"المركز: <b>{claim['rank']}</b> · النتيجة: <b>{claim['score']}/{claim['total_questions']}</b>\n\n"
        "هذه الشهادة تذكارية وغير قابلة للتحقق."
    )
    await bot.send_photo(
        chat_id=chat_id,
        photo=types.BufferedInputFile(png, filename=f"{filename}.png"),
        caption=caption,
        parse_mode="HTML",
    )
    await bot.send_document(
        chat_id=chat_id,
        document=types.BufferedInputFile(pdf, filename=f"{filename}.pdf"),
        caption="📄 نسخة PDF من الشهادة.",
    )

