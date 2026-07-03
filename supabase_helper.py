import os
import datetime
from dotenv import load_dotenv, find_dotenv
from supabase import create_client

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def check_or_add_user(user_id, username, referrer_id=None):
    """التحقق من المستخدم، إدارة الإحالات، ومنح النقاط اليومية تلقائياً"""
    today = datetime.date.today().isoformat()
    response = supabase.table("users").select("*").eq("user_id", user_id).execute()
    
    if not response.data:
        # 1. مستخدم جديد بالكامل
        actual_referrer = None
        # التأكد من أن رابط الإحالة ليس للمستخدم نفسه وأن المحيل موجود في النظام
        if referrer_id and str(referrer_id) != str(user_id):
            ref_check = supabase.table("users").select("points").eq("user_id", referrer_id).execute()
            if ref_check.data:
                actual_referrer = referrer_id
                # مكافأة المحيل بـ 10 نقاط إضافية
                new_ref_points = ref_check.data[0]['points'] + 10
                supabase.table("users").update({"points": new_ref_points}).eq("user_id", referrer_id).execute()
        
        # تسجيل المستخدم الجديد بـ 20 نقطة ترحيبية
        supabase.table("users").insert({
            "user_id": user_id, 
            "username": username, 
            "points": 20, 
            "total_questions": 0,
            "referred_by": actual_referrer,
            "last_renewal": today
        }).execute()
        
        return {"points": 20, "status": "new", "referrer": actual_referrer}
    
    # 2. مستخدم مسجل مسبقاً - فحص التجديد اليومي
    user_data = response.data[0]
    current_points = user_data['points']
    last_renewal = user_data.get('last_renewal')
    
    if last_renewal != today:
        # تجديد يومي: إضافة 15 نقطة مجانية فوق رصيده الحالي لليوم الجديد
        current_points += 15 
        supabase.table("users").update({
            "points": current_points,
            "last_renewal": today
        }).eq("user_id", user_id).execute()
        return {"points": current_points, "status": "renewed", "referrer": None}
        
    return {"points": current_points, "status": "normal", "referrer": None}

def update_user_stats(user_id, questions_generated):
    """خصم النقاط بعد توليد الأسئلة"""
    user = supabase.table("users").select("points, total_questions").eq("user_id", user_id).execute()
    if user.data:
        current_points = user.data[0]['points']
        current_total = user.data[0]['total_questions']
        new_points = max(0, current_points - questions_generated)
        new_total = current_total + questions_generated
        supabase.table("users").update({"points": new_points, "total_questions": new_total}).eq("user_id", user_id).execute()
        return new_points
    return None

# 🌟 دوال الآدمن الجديدة 🌟

def admin_add_points(target_id, amount):
    """دالة خاصة بالآدمن لشحن رصيد أي طالب"""
    user = supabase.table("users").select("points").eq("user_id", target_id).execute()
    if user.data:
        new_points = user.data[0]['points'] + amount
        supabase.table("users").update({"points": new_points}).eq("user_id", target_id).execute()
        return new_points
    return None

def admin_get_global_stats():
    """جلب إحصائيات البوت الكاملة من سوبابيس"""
    response = supabase.table("users").select("user_id, total_questions").execute()
    if response.data:
        total_users = len(response.data)
        total_questions = sum(user['total_questions'] for user in response.data)
        return {"total_users": total_users, "total_questions": total_questions}
    return {"total_users": 0, "total_questions": 0}

def admin_search_user(query):
    """البحث عن طالب برقم الآيدي أو اليوزرنيم لمعاينة بياناته"""
    if str(query).isdigit():
        res = supabase.table("users").select("*").eq("user_id", int(query)).execute()
    else:
        res = supabase.table("users").select("*").eq("username", query).execute()
    return res.data[0] if res.data else None