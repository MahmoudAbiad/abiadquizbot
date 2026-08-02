FROM python:3.11-slim

WORKDIR /app

# 1. تثبيت أدوات النظام وتنزيل ثنائي Tectonic المباشر مع التحقق من عمله
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    fontconfig \
    wget \
    tar \
    && wget -qO- https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-unknown-linux-musl.tar.gz | tar -xz -C /usr/local/bin/ \
    && chmod +x /usr/local/bin/tectonic \
    && tectonic --version \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. تنزيل خط "أميري" العربي وتثبيته في النظام
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

CMD ["python", "main.py"]