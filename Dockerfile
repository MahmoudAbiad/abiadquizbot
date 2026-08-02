FROM python:3.11-slim

WORKDIR /app

# 1. تثبيت أدوات النظام ومترجم Tectonic
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    fontconfig \
    && curl -fsSL https://drop-sh.tectonic-typesetting.github.io/installer/tectonic | sh -s -- --to /usr/local/bin \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. تنزيل خط "أميري" العربي مباشرة وتثبيته في النظام (لتفادي تقلبات مستودعات دبيان)
RUN mkdir -p /usr/share/fonts/truetype/amiri \
    && curl -fsSL "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf" -o /usr/share/fonts/truetype/amiri/Amiri-Regular.ttf \
    && curl -fsSL "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Bold.ttf" -o /usr/share/fonts/truetype/amiri/Amiri-Bold.ttf \
    && fc-cache -f -v

# 3. تثبيت مكتبات بايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. نسخ كود المشروع
COPY . .

# 5. إنشاء المجلدات المطلوبة
RUN mkdir -p downloads logs templates

# التشغيل
CMD ["python", "main.py"]