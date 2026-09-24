"""
Supabase database operations for user management and statistics.
Handles user registration, points management, database queries, centralized quiz caching, and community ratings.

هذا الملف (__init__.py) هو الواجهة الوحيدة (facade) لباكج supabase_helper: كل
الكود الفعلي انتقل لملفات فرعية أصغر حسب الموضوع (users.py, quiz_cache.py,
classification.py, ...) لتسهيل الصيانة، وهذا الملف بيجمعها ويعيد تصديرها بنفس
الأسماء القديمة بالضبط، حتى يضل أي مكان بالمشروع يستورد من `supabase_helper`
(أو `helpers.supabase_helper`) شغال تماماً بدون أي تعديل.

⚠️ ملاحظة: هذا الملف بيستورد فقط - لا تضيف أي منطق فعلي هون، وأي ملف فرعي
جديد لازم يستورد client/logger من `._client` حصراً (راجع تعليق _client.py).
"""

from ._client import (
    supabase,
    logger,
    SUPABASE_URL,
    _is_valid_uuid,
    dotenv_path,
)

from .users import (
    _balance_payload,
    check_or_add_user,
    _add_new_user,
    reward_referrer_if_eligible,
    _check_daily_renewal,
    update_user_stats,
    refund_user_points,
)

from .quiz_cache import (
    _is_transient_jwt_clock_skew_error,
    get_file_quizzes,
    save_file_quiz_multiple,
    log_ai_generation,
    _pricing_cache,
    _pricing_cache_timestamp,
    _PRICING_CACHE_TTL_SECONDS,
    get_ai_model_pricing,
    get_cached_quiz,
    save_quiz_to_cache,
)

from .quiz_images import (
    QUIZ_IMAGES_BUCKET,
    upload_quiz_question_image,
    save_question_image_url,
    get_quiz_creator_id,
    update_quiz_question,
)

from .sharing import (
    create_shared_quiz_id,
    save_shared_quiz,
    get_shared_quiz,
)

from .favorites import (
    count_favorite_sections,
    list_favorite_sections,
    create_favorite_section,
    save_favorite_quiz,
    list_favorite_quizzes,
    can_create_more_favorite_sections,
    get_favorite_quiz,
    get_favorite_quiz_by_global_id,
    remove_favorite_quiz,
)

from .classification import (
    get_classification_lock,
    has_user_voted_on_classification,
    submit_classification_vote,
    _get_file_hashes_for_quiz_ids,
    _cleanup_classification_for_hashes,
    _get_safe_to_delete_quiz_ids,
    auto_cleanup_bad_quizzes,
)

from .feedback import (
    admin_get_feedbacks_page,
    admin_get_feedback_by_id,
    admin_get_quiz_board_position,
    admin_get_quiz_by_id,
    admin_delete_quiz,
    submit_quiz_vote,
    save_quiz_feedback,
)

from .feature_flags import (
    _FEATURE_FLAG_REDIS_PREFIX,
    FEATURE_FLAG_CACHE_TTL_SECONDS,
    _flag_redis_key,
    _decode_flag_value,
    is_feature_enabled,
    set_feature_flag,
    get_all_feature_flags,
)

from .admin_ops import (
    admin_add_points,
    admin_get_global_stats,
    admin_search_user,
)

from .leaderboard import (
    get_or_update_high_score,
    publish_score_to_leaderboard,
    hide_score_from_leaderboard,
    get_my_leaderboard_status,
    get_top_5_leaderboard,
)

from .analytics import (
    log_usage_event,
    log_error_event,
    flush_analytics_queue,
    start_quiz_attempt,
    _insert_quiz_attempt,
    complete_quiz_attempt,
    has_completed_any_quiz_before,
    mark_quiz_attempt_stopped,
    admin_get_usage_overview,
    admin_get_daily_active_users,
    admin_get_user_activity,
    admin_get_all_usage_events,
    admin_get_recent_errors,
    admin_get_quiz_generation_log,
    admin_get_referral_leaderboard,
    admin_get_today_active_users,
    admin_get_today_quizzes,
    USER_QUIZZES_FETCH_CAP,
    admin_get_user_quizzes,
    auto_cleanup_old_analytics_data,
)

from .storage_files import _create_signed_upload_target

from .storage_audio import (
    create_audio_upload_target,
    get_audio_temp_object_size,
    download_audio_temp_to_file,
    delete_audio_temp,
    cleanup_stale_audio_uploads,
)

from .storage_files import (
    create_file_upload_target,
    create_image_upload_targets,
    get_file_temp_object_size,
    download_file_temp_to_file,
    delete_file_temp,
    delete_file_temp_batch,
    cleanup_stale_file_uploads,
)

__all__ = [
    # _client
    "supabase", "logger", "SUPABASE_URL", "_is_valid_uuid", "dotenv_path",
    # users
    "_balance_payload", "check_or_add_user", "_add_new_user",
    "reward_referrer_if_eligible", "_check_daily_renewal",
    "update_user_stats", "refund_user_points",
    # quiz_cache
    "_is_transient_jwt_clock_skew_error", "get_file_quizzes",
    "save_file_quiz_multiple", "log_ai_generation",
    "_pricing_cache", "_pricing_cache_timestamp", "_PRICING_CACHE_TTL_SECONDS",
    "get_ai_model_pricing", "get_cached_quiz", "save_quiz_to_cache",
    # quiz_images
    "QUIZ_IMAGES_BUCKET", "upload_quiz_question_image",
    "save_question_image_url", "get_quiz_creator_id", "update_quiz_question",
    # sharing
    "create_shared_quiz_id", "save_shared_quiz", "get_shared_quiz",
    # favorites
    "count_favorite_sections", "list_favorite_sections",
    "create_favorite_section", "save_favorite_quiz", "list_favorite_quizzes",
    "can_create_more_favorite_sections", "get_favorite_quiz",
    "get_favorite_quiz_by_global_id", "remove_favorite_quiz",
    # classification
    "get_classification_lock", "has_user_voted_on_classification",
    "submit_classification_vote", "_get_file_hashes_for_quiz_ids",
    "_cleanup_classification_for_hashes", "_get_safe_to_delete_quiz_ids",
    "auto_cleanup_bad_quizzes",
    # feedback
    "admin_get_feedbacks_page", "admin_get_feedback_by_id",
    "admin_get_quiz_board_position", "admin_get_quiz_by_id",
    "admin_delete_quiz", "submit_quiz_vote", "save_quiz_feedback",
    # feature_flags
    "_FEATURE_FLAG_REDIS_PREFIX", "FEATURE_FLAG_CACHE_TTL_SECONDS",
    "_flag_redis_key", "_decode_flag_value",
    "is_feature_enabled", "set_feature_flag", "get_all_feature_flags",
    # admin_ops
    "admin_add_points", "admin_get_global_stats", "admin_search_user",
    # leaderboard
    "get_or_update_high_score", "publish_score_to_leaderboard",
    "hide_score_from_leaderboard", "get_my_leaderboard_status",
    "get_top_5_leaderboard",
    # analytics
    "log_usage_event", "log_error_event", "flush_analytics_queue",
    "start_quiz_attempt", "_insert_quiz_attempt", "complete_quiz_attempt",
    "has_completed_any_quiz_before", "mark_quiz_attempt_stopped",
    "admin_get_usage_overview", "admin_get_daily_active_users",
    "admin_get_user_activity", "admin_get_all_usage_events",
    "admin_get_recent_errors", "admin_get_quiz_generation_log",
    "admin_get_referral_leaderboard", "admin_get_today_active_users",
    "admin_get_today_quizzes", "USER_QUIZZES_FETCH_CAP",
    "admin_get_user_quizzes", "auto_cleanup_old_analytics_data",
    # storage_audio
    "create_audio_upload_target", "get_audio_temp_object_size",
    "download_audio_temp_to_file", "delete_audio_temp",
    "cleanup_stale_audio_uploads",
    # storage_files
    "_create_signed_upload_target",
    "create_file_upload_target", "create_image_upload_targets",
    "get_file_temp_object_size", "download_file_temp_to_file",
    "delete_file_temp", "delete_file_temp_batch",
    "cleanup_stale_file_uploads",
]
