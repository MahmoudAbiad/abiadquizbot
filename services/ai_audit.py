"""Provenance/operation metadata stored in existing usage_events JSON."""


def input_channel_label(channel):
    return {'telegram': '💬 محادثة تلغرام', 'web_upload': '🌐 صفحة رفع الويب'}.get(channel, 'غير مسجَّل')


def build_audio_audit_metadata(metadata, operation, input_channel, *, duration_minutes, cost,
                             truncated=False, duration_seconds=None):
    metadata = metadata or {}
    result = {
        'operation_type': operation,
        'content_source': 'audio' if operation == 'audio_transcription' else 'audio_transcript',
        'input_channel': input_channel,
        'duration_minutes': duration_minutes,
        'cost': cost,
        'truncated': truncated,
        'generation_mode': operation,
        'generation_seconds': duration_seconds if duration_seconds is not None else metadata.get('duration_seconds'),
        'ai_provider': metadata.get('provider'),
        'ai_model': metadata.get('model'),
        'cascade_rank': metadata.get('cascade_rank'),
        'cascade_total': metadata.get('cascade_total'),
    }
    for name in ('input_tokens', 'output_tokens', 'thoughts_tokens', 'total_tokens'):
        if name in metadata:
            result[name] = metadata[name]
    return result
