FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create downloads and logs directories
RUN mkdir -p downloads logs

# 🩹 FIX (memory-leak): يمنع glibc من فتح arena ذاكرة منفصل لكل thread (كل arena
# قد يحجز عشرات الـ MB خاصة به دون مشاركتها مع باقي البرنامج) - مهم هنا لأن
# asyncio.to_thread يُشغّل matplotlib/fitz/reportlab على عدة threads فعلية.
# MALLOC_TRIM_THRESHOLD_ أقل من الافتراضي (128KB بدل ~128MB الديناميكي) يجعل
# glibc يعيد الصفحات الفارغة الكبيرة لنظام التشغيل بشكل أكثر عدوانية.
ENV MALLOC_ARENA_MAX=2
ENV MALLOC_TRIM_THRESHOLD_=131072

# Run the bot
CMD ["python", "main.py"]
