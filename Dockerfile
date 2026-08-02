FROM python:3.11-slim

WORKDIR /app

# 1. تثبيت أدوات النظام والخطوط العربية ومترجم Tectonic
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    fontconfig \
    fonts-amiri \
    && curl -fsSL https://drop-sh.tectonic-typesetting.github.io/installer/tectonic | sh -s -- --to /usr/local/bin \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. تثبيت مكتبات بايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. نسخ كود المشروع
COPY . .

# 4. إنشاء المجلدات المطلوبة (إضافة templates)
RUN mkdir -p downloads logs templates

# التشغيل
CMD ["python", "main.py"]