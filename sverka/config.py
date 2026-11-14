"""Настройки. Все параметры задаются переменными окружения (см. README)."""
import os
from decimal import Decimal

# Допуск при сравнении сумм, руб. Расхождение не больше допуска считается округлением.
TOLERANCE = Decimal(os.getenv("SVERKA_TOLERANCE", "0.01"))
# Языки Tesseract для обычных документов.
OCR_LANG = os.getenv("SVERKA_OCR_LANG", "rus+eng")
# Разрешение растеризации PDF для OCR.
OCR_DPI = int(os.getenv("SVERKA_OCR_DPI", "300"))
# Разрешение изображений страниц PDF с текстовым слоем (доказательства и просмотр страницы).
RENDER_DPI = int(os.getenv("SVERKA_RENDER_DPI", "150"))
# Каталог с моделями Tesseract (необязательно, например tessdata_best).
TESSDATA_DIR = os.getenv("SVERKA_TESSDATA_DIR", "")
# Необязательная локальная LLM (OpenAI-совместимый API, например Ollama) для неоднозначной классификации.
LLM_URL = os.getenv("SVERKA_LLM_URL", "").rstrip("/")
LLM_MODEL = os.getenv("SVERKA_LLM_MODEL", "qwen2.5:7b-instruct")
LLM_TIMEOUT = float(os.getenv("SVERKA_LLM_TIMEOUT", "60"))
# Порог уверенности правил, ниже которого документ считается неоднозначным.
CLASSIFY_MIN_CONF = float(os.getenv("SVERKA_CLASSIFY_MIN_CONF", "0.34"))
# Каталог для результатов веб-интерфейса.
DATA_DIR = os.getenv("SVERKA_DATA_DIR", "/tmp/sverka-data")
# Максимальный размер загружаемого архива, МБ.
MAX_UPLOAD_MB = int(os.getenv("SVERKA_MAX_UPLOAD_MB", "200"))
# Ограничения для zip-архивов: число файлов и общий объем после распаковки, МБ.
MAX_ARCHIVE_FILES = int(os.getenv("SVERKA_MAX_ARCHIVE_FILES", "500"))
MAX_ARCHIVE_MB = int(os.getenv("SVERKA_MAX_ARCHIVE_MB", "300"))
# Режим запуска проверок через API: "live" — полная проверка (подготовка, модель, валидация, сверка), нужны
# настройки SVERKA_PROVIDER_*; "disabled" — проверка сразу завершается понятной ошибкой; "fixture" — только для
# разработки интерфейса: проверка завершается синтетическим примером результата с явной пометкой.
RUN_MODE = os.getenv("SVERKA_RUN_MODE", "live")
# Если в наборе есть нечитаемый файл, расхождение «отчет больше документов» считается неподтвержденным
# (недостаток данных). Значение 0 отключает это правило: расхождение будет показано как ошибка отчета.
UNREADABLE_BLOCKS = os.getenv("SVERKA_UNREADABLE_BLOCKS", "1") not in ("0", "false", "no")
