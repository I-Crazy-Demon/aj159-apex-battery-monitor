#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AJ159 APEX Battery Monitor -- монитор заряда докстанции мыши Ajazz AJ159 APEX
для Windows 10/11 (иконка в трее, уведомления, синхронизация часов
докстанции).

============================================================================
О ПРОТОКОЛЕ
============================================================================
Официальной документации на HID-протокол AJ159 APEX не существует. Всё,
что описано ниже, получено перехватом реального обмена сайта-конфигуратора
https://qmk.top через браузерный WebHID API (см. README.md, раздел
"Как был получен протокол") и проверено на реальном устройстве.

Устройство:
    VID 0x3151, PID 0x5007, вендорский HID-интерфейс с usage_page 0xffff
    (таких интерфейсов два -- код перебирает оба и использует рабочий,
    не полагаясь на фиксированный interface_number).

Транспорт:
    HID Feature Report, report ID = 0, все пакеты по 64 байта.

Команда "статус" (0xf7):
    OUT: byte[0]=0xf7, остальное 0x00
    IN:  byte[0..1]=0x00 (константа)
         byte[2]     = процент заряда, 0-100 (готовое число, не сырой ADC)
         byte[3]     = байт не расшифрован (не индикатор "на базе" --
                        не менялся при снятии мыши с базы в тестах)
         byte[4..]   = таблица кнопок + таблица RGB-цветов (не используется)

Команда "установить время" (0x28):
    OUT: byte[0]=0x28, byte[1..6]=0x00, byte[7]=0xFF-byte[0] (контрольная
         сумма-дополнение -- подтверждена на трёх разных командах:
         0x28/0xd3/0xd4, везде byte[7] = 0xFF - byte[0]),
         byte[8]=год>>8, byte[9]=год&0xFF, byte[10]=месяц, byte[11]=день,
         byte[12]=час(24ч), byte[13]=минута, byte[14]=секунда,
         остальное 0x00. Ответа устройство не даёт.

Команда "очистить экран" (0xac), перехват qmk.top от 2026-09-20:
    Feature Report ID 0, 64 байта полезной нагрузки:
    byte[0]=0xac, byte[7]=0x53, остальные байты нулевые.
    Вызывается вручную из меню трея для удаления картинки с экрана.
    Успешная отправка не подтверждает визуальное состояние экрана.

ВАЖНО: экран докстанции в простое (без движения мыши >20 сек)
принудительно показывает статус радиосвязи (крестик/кружок) поверх
ЛЮБОГО контента -- это не настраиваемый "скринсейвер", а прошивка.
Обойти это программно нельзя (проверено).

============================================================================
УСТАНОВКА
============================================================================
    pip install hidapi pystray pillow plyer

============================================================================
ИСПОЛЬЗОВАНИЕ
============================================================================
    ajazz_battery.py check
        Разовая проверка: сырой ответ устройства + разобранный процент.

    ajazz_battery.py sync-time
        Разовая синхронизация часов докстанции с системным временем ПК.

    ajazz_battery.py monitor [опции]
        Фоновый монитор: иконка-батарейка в системном трее, уведомления
        о низком заряде, автоматическая синхронизация времени докстанции.

        --interval N        Опрос заряда раз в N секунд (по умолчанию 120)
        --threshold N        Порог уведомления о низком заряде, % (по умолчанию 20)
        --sync-interval N    Синхронизация времени раз в N секунд (по умолчанию 3600)

    ajazz_battery.py help | --help | -h | /?
        Показать это описание.

Примеры:
    ajazz_battery.py monitor
    ajazz_battery.py monitor --interval 300 --threshold 15 --sync-interval 1800

Если устройство не отвечает -- закройте вкладку с qmk.top в браузере
(WebHID может удерживать интерфейс) и повторите попытку.
"""

import sys
import time
import threading
import datetime
import argparse


# ============================================================================
# Константы протокола
# ============================================================================

VENDOR_ID = 0x3151
PRODUCT_ID = 0x5007
USAGE_PAGE_VENDOR = 0xFFFF

CMD_STATUS = 0xF7
CMD_SET_TIME = 0x28
CMD_CLEAR_SCREEN = 0xAC

REPORT_SIZE = 64          # размер полезной нагрузки HID Feature Report
WINDOWS_REPORT_ID_PAD = 1  # доп. байт report-id, который добавляет hidapi на Windows

# Цветовая схема иконки трея по уровню заряда (в процентах, включительно)
COLOR_GREEN = (60, 170, 80, 255)     # 100-50%
COLOR_YELLOW = (230, 180, 40, 255)   # 49-15%
COLOR_RED = (220, 50, 50, 255)       # 14-0% (ниже 5% -- мигает)
COLOR_GRAY = (110, 110, 110, 255)    # нет данных
BLINK_THRESHOLD = 5

# Контроль выхода Windows из сна/гибернации. Поток наблюдения просыпается
# каждые 5 секунд; пауза в 15+ секунд означает, что выполнение программы
# было приостановлено. После этого выполняются 10 попыток с паузами
# по 3 секунды; длительность HID-вызовов добавляется ко времени ожидания.
RESUME_CHECK_INTERVAL = 5
RESUME_GAP_THRESHOLD = 15
RESUME_SYNC_RETRY_INTERVAL = 3
RESUME_SYNC_RETRIES = 10

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\seguisb.ttf",   # Segoe UI Semibold
    r"C:\Windows\Fonts\segoeuib.ttf",  # Segoe UI Bold
    r"C:\Windows\Fonts\arialbd.ttf",   # Arial Bold
    r"C:\Windows\Fonts\arial.ttf",
]

ICON_CANVAS_SIZE = 128  # рисуем крупно, Windows сама уменьшит под трей


def _import_hid():
    """Отложенный импорт hidapi -- чтобы --help и парсинг аргументов
    работали даже без установленных зависимостей."""
    try:
        import hid
        return hid
    except ImportError:
        print("Не найден модуль 'hid'. Установите: pip install hidapi",
              file=sys.stderr)
        sys.exit(1)


# ============================================================================
# Протокол устройства: сборка пакетов, поиск и опрос интерфейса
# ============================================================================

def find_vendor_interfaces(hid_module):
    """Возвращает список HID-интерфейсов устройства с нужным usage_page,
    не полагаясь на фиксированный interface_number (может отличаться
    между машинами и версиями прошивки)."""
    return [
        d for d in hid_module.enumerate(VENDOR_ID, PRODUCT_ID)
        if d.get('usage_page') == USAGE_PAGE_VENDOR
    ]


def build_status_query():
    """64-байтный запрос статуса устройства (заряд, кнопки, цвета)."""
    return bytes([CMD_STATUS]) + bytes(REPORT_SIZE - 1)


def build_time_sync_packet(dt=None):
    """64-байтный пакет установки времени на экране докстанции.
    См. описание формата в docstring модуля."""
    if dt is None:
        dt = datetime.datetime.now()

    packet = bytearray(REPORT_SIZE)
    packet[0] = CMD_SET_TIME
    packet[7] = 0xFF - CMD_SET_TIME
    packet[8] = (dt.year >> 8) & 0xFF
    packet[9] = dt.year & 0xFF
    packet[10] = dt.month
    packet[11] = dt.day
    packet[12] = dt.hour
    packet[13] = dt.minute
    packet[14] = dt.second
    return bytes(packet)


def build_clear_screen_packet():
    """Точный пакет очистки из перехвата WebHID от 2026-09-20."""
    packet = bytearray(REPORT_SIZE)
    packet[0] = CMD_CLEAR_SCREEN
    packet[7] = 0x53
    return bytes(packet)


def send_clear_screen(h):
    """Отправляет очистку картинки; результат проверяется на экране."""
    send_feature(h, build_clear_screen_packet())


def send_feature(h, payload):
    """Отправляет Feature Report с учётом report-id байта, который
    требует hidapi на Windows."""
    h.send_feature_report(bytes([0x00]) + payload)


def read_feature(h, size=REPORT_SIZE):
    """Читает Feature Report; возвращает size+1 байт (report-id + данные)."""
    return bytes(h.get_feature_report(0x00, size + WINDOWS_REPORT_ID_PAD))


def read_status(h):
    """Запрашивает статус устройства.

    Возвращает (percent, unknown_byte, raw_response):
        percent      -- заряд, 0-100, либо None если ответ не распознан
        unknown_byte -- байт[3] ответа, значение пока не расшифровано
        raw_response -- сырые байты ответа, для диагностики
    """
    send_feature(h, build_status_query())
    resp = read_feature(h)

    if len(resp) < 5:
        return None, None, resp

    # resp[0] -- эхо report-id (0x00) на Windows, дальше идут байты устройства.
    percent = resp[3] if resp[3] <= 100 else None
    unknown_byte = resp[4]
    return percent, unknown_byte, resp


def send_time_sync(h, dt=None):
    """Отправляет команду установки времени. Устройство не отвечает."""
    dt = dt or datetime.datetime.now()
    send_feature(h, build_time_sync_packet(dt))
    return dt


def open_working_device(hid_module):
    """Перебирает вендорские интерфейсы устройства и возвращает первый,
    который отвечает на запрос статуса правдоподобным процентом (0-100).

    Возвращает (handle, device_info). Поднимает RuntimeError, если
    ни один интерфейс не найден или не ответил."""
    candidates = find_vendor_interfaces(hid_module)
    if not candidates:
        raise RuntimeError(
            f"Вендорский интерфейс AJ159 APEX (VID {VENDOR_ID:04x}:"
            f"{PRODUCT_ID:04x}, usage_page {USAGE_PAGE_VENDOR:#x}) не "
            "найден. Проверьте подключение докстанции."
        )

    last_error = None
    for info in candidates:
        h = hid_module.device()
        try:
            h.open_path(info['path'])
            h.set_nonblocking(0)
            percent, _, _ = read_status(h)
            if percent is not None and 0 <= percent <= 100:
                return h, info
            h.close()
        except Exception as e:
            last_error = e
            try:
                h.close()
            except Exception:
                pass

    raise RuntimeError(
        f"Ни один из {len(candidates)} вендорских интерфейсов не дал "
        f"правдоподобного ответа. Последняя ошибка: {last_error}. "
        "Закройте qmk.top в браузере (может удерживать интерфейс) и "
        "попробуйте снова."
    )


# ============================================================================
# Команда: check
# ============================================================================

def cmd_check():
    hid_module = _import_hid()
    print("Ищу и открываю рабочий интерфейс...")
    h, info = open_working_device(hid_module)
    print(f"Открыт интерфейс: path={info['path'].decode(errors='replace')}, "
          f"interface_number={info.get('interface_number')}")

    try:
        percent, unknown_byte, raw = read_status(h)
    finally:
        h.close()

    hex_str = ' '.join(f'{b:02x}' for b in raw)
    print(f"\nСырой ответ: {hex_str}")
    print(f"\nЗаряд: {percent}%")
    print(f"Байт[4] (не расшифрован, ранее считался 'заряжается'): "
          f"{unknown_byte}")
    print("\nПримечание: в тестах этот байт не менялся при снятии мыши "
          "с базы -- скорее всего, НЕ индикатор 'на базе'. Мониторинг "
          "заряда полагается только на процент.")


# ============================================================================
# Команда: sync-time
# ============================================================================

def cmd_sync_time():
    hid_module = _import_hid()
    print("Ищу и открываю рабочий интерфейс...")
    h, info = open_working_device(hid_module)
    try:
        now = send_time_sync(h)
    finally:
        h.close()
    print(f"Отправлена команда установки времени: "
          f"{now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("\nПроверьте визуально на экранчике докстанции, что дата и время "
          "совпадают. Устройство не подтверждает приём этой команды "
          "программно -- только визуальная проверка.")


# ============================================================================
# Команда: monitor
# ============================================================================

def _load_font(size):
    """Пробует системные жирные шрифты Windows, иначе -- мелкий
    встроенный шрифт Pillow."""
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _level_color(percent):
    if percent is None:
        return COLOR_GRAY
    if percent >= 50:
        return COLOR_GREEN
    if percent >= 15:
        return COLOR_YELLOW
    return COLOR_RED


def _is_blinking_level(percent):
    return percent is not None and percent < BLINK_THRESHOLD


def make_battery_icon(percent, blink_on=True):
    """Рисует иконку-батарейку (горизонтальная ориентация) с числом
    заряда внутри. При percent < BLINK_THRESHOLD и blink_on=False
    заливка не рисуется -- используется для мигания."""
    from PIL import Image, ImageDraw

    img = Image.new('RGBA', (ICON_CANVAS_SIZE, ICON_CANVAS_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Геометрия: корпус слева, "клемма" справа -- горизонтальная
    # ориентация даёт больше места по ширине под цифры.
    body_left, body_right = 6, 106
    body_top, body_bottom = 32, 96
    nub_left, nub_right = 106, 122
    nub_top, nub_bottom = 50, 78
    border = 6
    outline_color = (235, 235, 235, 255)

    draw.rounded_rectangle((body_left, body_top, body_right, body_bottom),
                            radius=10, outline=outline_color, width=border)
    draw.rounded_rectangle((nub_left, nub_top, nub_right, nub_bottom),
                            radius=4, fill=outline_color)

    show_fill = True
    if _is_blinking_level(percent):
        show_fill = blink_on

    if percent is not None and show_fill:
        color = _level_color(percent)
        pad = border + 4
        inner_left, inner_right = body_left + pad, body_right - pad
        inner_top, inner_bottom = body_top + pad, body_bottom - pad
        fill_w = (inner_right - inner_left) * (percent / 100.0)
        if fill_w >= 2:
            draw.rounded_rectangle(
                (inner_left, inner_top, inner_left + fill_w, inner_bottom),
                radius=4, fill=color
            )

    label = str(percent) if percent is not None else "?"
    font_size = 46 if len(label) <= 2 else 38  # "100" длиннее -- уменьшаем
    font = _load_font(font_size)

    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = body_left + ((body_right - body_left) - tw) / 2 - bbox[0]
    ty = body_top + ((body_bottom - body_top) - th) / 2 - bbox[1]

    outline_w = 3  # тёмная обводка текста для читаемости на любом фоне
    for dx in range(-outline_w, outline_w + 1):
        for dy in range(-outline_w, outline_w + 1):
            if dx or dy:
                draw.text((tx + dx, ty + dy), label, font=font, fill=(0, 0, 0, 255))
    draw.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))

    return img


class TrayMonitor:
    """Фоновый монитор заряда и часов докстанции с иконкой в трее.

    Четыре независимых потока:
      - poll: периодически опрашивает заряд через HID, обновляет иконку
      - blink: перерисовывает иконку раз в 0.8с при критическом заряде
               (без обращений к HID -- только по уже известному значению)
      - sync: синхронизирует часы докстанции сразу при старте и затем
              периодически
      - resume: обнаруживает пробуждение Windows и повторно синхронизирует
                время после появления USB-докстанции
    """

    def __init__(self, hid_module, interval, threshold, sync_interval):
        self.hid_module = hid_module
        self.interval = interval
        self.threshold = threshold
        self.sync_interval = sync_interval

        self.state_lock = threading.Lock()
        # hidapi и само устройство не рассчитаны на параллельные команды.
        # Общая блокировка сериализует опрос, синхронизацию и ручные действия.
        self.hid_lock = threading.Lock()
        self.manual_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.percent = None
        self.last_error = None
        self.alerted = False

        try:
            from plyer import notification as plyer_notification
            self._plyer = plyer_notification
        except ImportError:
            self._plyer = None
            print("Модуль plyer не найден (pip install plyer) -- "
                  "уведомления будут выводиться только в консоль.")

        self.icon = None  # создаётся в run()

    # -- вспомогательное -----------------------------------------------

    def _notify(self, title, message):
        if self.stop_event.is_set():
            return
        print(f"[УВЕДОМЛЕНИЕ] {title}: {message}")
        if self._plyer is not None:
            try:
                self._plyer.notify(title=title, message=message,
                                    app_name="AJ159 APEX Battery", timeout=10)
            except Exception as e:
                print(f"Не удалось показать toast-уведомление: {e}")

    def _refresh_icon(self):
        if self.stop_event.is_set():
            return
        with self.state_lock:
            p = self.percent
        self.icon.icon = make_battery_icon(p, blink_on=True)
        self.icon.title = (f"AJ159 APEX: {p}%" if p is not None
                            else "AJ159 APEX: нет данных")

    # -- опрос заряда ----------------------------------------------------

    def _poll_once(self):
        try:
            with self.hid_lock:
                if self.stop_event.is_set():
                    return
                h = None
                try:
                    h, _ = open_working_device(self.hid_module)
                    percent, _, _ = read_status(h)
                    if percent is None:
                        raise RuntimeError("Нераспознанный ответ статуса")
                finally:
                    if h is not None:
                        h.close()
            with self.state_lock:
                self.percent = percent
                self.last_error = None
        except Exception as e:
            with self.state_lock:
                self.last_error = str(e)
                self.percent = None
            self._refresh_icon()
            print(f"Ошибка опроса: {e}")
            return

        self._refresh_icon()

        with self.state_lock:
            p = self.percent
            notify = p is not None and p <= self.threshold and not self.alerted
            if notify:
                self.alerted = True
            elif p is not None and p > self.threshold + 5:
                self.alerted = False
        if notify:
            self._notify("Низкий заряд мыши",
                         f"AJ159 APEX: осталось {p}%. Пора на зарядку.")

    def _poll_loop(self):
        while not self.stop_event.is_set():
            self._poll_once()
            self.stop_event.wait(self.interval)

    def _blink_loop(self):
        blink_on = True
        while not self.stop_event.is_set():
            with self.state_lock:
                p = self.percent
            if _is_blinking_level(p):
                self.icon.icon = make_battery_icon(p, blink_on=blink_on)
                blink_on = not blink_on
                self.stop_event.wait(0.8)
            else:
                self.stop_event.wait(1.0)

    # -- синхронизация времени -------------------------------------------

    def _do_sync_time(self):
        try:
            with self.hid_lock:
                if self.stop_event.is_set():
                    return
                h = None
                try:
                    h, _ = open_working_device(self.hid_module)
                    now = send_time_sync(h)
                finally:
                    if h is not None:
                        h.close()
            print(f"Время докстанции синхронизировано: "
                  f"{now.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
        except Exception as e:
            print(f"Не удалось синхронизировать время: {e}")
            return False

    def _sync_time_loop(self):
        self._do_sync_time()  # сразу при старте
        while not self.stop_event.is_set():
            self.stop_event.wait(self.sync_interval)
            if not self.stop_event.is_set():
                self._do_sync_time()

    def _sync_after_resume(self):
        """После пробуждения повторяет синхронизацию, пока USB-докстанция
        заново определяется Windows. До 10 попыток, между ними 3 секунды ожидания;
        длительность HID-вызовов добавляется к этому времени."""
        for attempt in range(1, RESUME_SYNC_RETRIES + 1):
            if self.stop_event.is_set():
                return
            if self._do_sync_time():
                print("Время повторно синхронизировано после выхода из сна.")
                return
            if attempt < RESUME_SYNC_RETRIES:
                if self.stop_event.wait(RESUME_SYNC_RETRY_INTERVAL):
                    return
        print("Докстанция не появилась после выхода из сна; "
              "синхронизация будет повторена по обычному расписанию.")

    def _resume_watch_loop(self):
        """Обнаруживает длительную приостановку процесса.

        На Windows time.monotonic() продолжает учитывать время сна, а поток
        программы в это время не выполняется. Поэтому большой разрыв между
        двумя проверками надёжно указывает на сон/гибернацию. Ложное
        срабатывание из-за сильной нагрузки безопасно: оно лишь повторно
        установит правильное время.
        """
        last_check = time.monotonic()
        while not self.stop_event.wait(RESUME_CHECK_INTERVAL):
            now = time.monotonic()
            gap = now - last_check
            last_check = now
            if gap >= RESUME_GAP_THRESHOLD:
                print(f"Обнаружено возобновление работы после паузы "
                      f"{gap:.0f} сек.")
                self._sync_after_resume()
                # Не считать время, потраченное на повторы, новой паузой.
                last_check = time.monotonic()

    # -- меню трея ---------------------------------------------------

    def _start_manual(self, action):
        """Не блокировать меню на USB и не накапливать ручные команды."""
        if self.stop_event.is_set() or not self.manual_lock.acquire(False):
            return
        def worker():
            try:
                if not self.stop_event.is_set():
                    action()
            except Exception as e:
                print(f"Ошибка ручной операции: {e}")
            finally:
                self.manual_lock.release()
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self.manual_lock.release()
            raise

    def _on_refresh(self, icon, item):
        self._start_manual(self._poll_once)

    def _on_show_exact(self, icon, item):
        with self.state_lock:
            p, err = self.percent, self.last_error
        if p is not None:
            self._notify("Точный заряд AJ159 APEX", f"{p}%")
        else:
            self._notify("AJ159 APEX", f"Данные недоступны. {err or ''}")

    def _on_sync_time_now(self, icon, item):
        self._start_manual(self._sync_time_manual)

    def _sync_time_manual(self):
        if self._do_sync_time():
            self._notify("AJ159 APEX", "Время докстанции синхронизировано.")
        else:
            self._notify("AJ159 APEX",
                         "Не удалось синхронизировать время докстанции.")

    def _on_clear_screen(self, icon, item):
        self._start_manual(self._clear_screen_manual)

    def _clear_screen_manual(self):
        try:
            with self.hid_lock:
                if self.stop_event.is_set():
                    return
                h = None
                try:
                    h, _ = open_working_device(self.hid_module)
                    send_clear_screen(h)
                finally:
                    if h is not None:
                        h.close()
        except Exception as e:
            print(f"Не удалось очистить экран докстанции: {e}")
            self._notify("AJ159 APEX",
                         "Не удалось отправить команду очистки экрана.")
            return
        self._notify("AJ159 APEX",
                     "Команда очистки отправлена. Появление часов может занять около 35 секунд.")

    def _on_quit(self, icon, item):
        self.stop_event.set()
        icon.stop()

    # -- запуск --------------------------------------------------------

    def _setup(self, icon):
        """Вызывается pystray ПОСЛЕ того, как значок реально появился в
        трее. Запускать фоновые потоки раньше нельзя: их первое обновление
        иконки/тайтла тихо терялось бы, пока значка ещё физически нет --
        внешне это выглядело как застрявший '?' до первого ручного
        обновления или до истечения полного --interval."""
        icon.visible = True
        for target in (self._poll_loop, self._blink_loop,
                       self._sync_time_loop, self._resume_watch_loop):
            threading.Thread(target=target, daemon=True).start()

    def run(self):
        import pystray

        self.icon = pystray.Icon(
            "aj159_apex_battery",
            make_battery_icon(None),
            "AJ159 APEX: опрашиваю...",
            menu=pystray.Menu(
                pystray.MenuItem("Обновить сейчас", self._on_refresh),
                pystray.MenuItem("Показать точный заряд", self._on_show_exact),
                pystray.MenuItem("Синхронизировать время докстанции",
                                  self._on_sync_time_now),
                pystray.MenuItem("Очистить экран докстанции",
                                  self._on_clear_screen),
                pystray.MenuItem("Выход", self._on_quit),
            )
        )

        print(f"Монитор запущен. Опрос заряда каждые {self.interval} сек, "
              f"порог уведомления {self.threshold}%. Синхронизация времени "
              f"докстанции каждые {self.sync_interval} сек (и сразу при "
              f"старте). Иконка в системном трее.")
        self.icon.run(setup=self._setup)


def cmd_monitor(interval, threshold, sync_interval):
    hid_module = _import_hid()
    try:
        import pystray          # noqa: F401 -- проверка наличия зависимости
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except ImportError:
        print("Для режима monitor нужны pystray и pillow: "
              "pip install pystray pillow")
        sys.exit(1)

    TrayMonitor(hid_module, interval, threshold, sync_interval).run()


# ============================================================================
# CLI
# ============================================================================

HELP_ALIASES = {'help', '/?', '-?', '?'}  # argparse уже понимает -h/--help сам


class _QuietArgumentParser(argparse.ArgumentParser):
    """При ошибке разбора аргументов (неизвестная команда/флаг) выводит
    только строку usage, без детального 'invalid choice: ...' и списка
    допустимых значений -- по вашему пожеланию: любой нераспознанный
    ввод обрабатывается единообразно, минимальным сообщением."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.exit(2)


def _ensure_console_for_cli():
    """PyInstaller-сборка с --noconsole не имеет консоли вообще: sys.stdout
    в ней -- заглушка, print() отправляется в никуда без ошибок и без
    видимого эффекта. Это осознанно для команды 'monitor' (не должно
    мелькать окно), но ломает вывод для 'check'/'sync-time'/'help'.

    Здесь мы пытаемся прицепиться к консоли процесса-родителя (если
    запущены из уже открытого cmd/PowerShell) или, если родителя-консоли
    нет (например, двойной клик по exe), создаём новое консольное окно.

    Ничего не делает для обычного запуска через python.exe -- там
    консоль уже есть и трогать нечего.

    Возвращает True, если было создано НОВОЕ окно консоли (в этом случае
    вызывающий код должен сделать паузу перед выходом, иначе окно
    закроется мгновенно вместе с процессом, не дав прочитать вывод).
    """
    if not getattr(sys, 'frozen', False):
        return False

    import ctypes
    kernel32 = ctypes.windll.kernel32
    ATTACH_PARENT_PROCESS = -1

    attached = kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
    allocated_new = False
    if not attached:
        kernel32.AllocConsole()
        allocated_new = True

    # После Attach/AllocConsole нужно явно перепривязать stdout/stderr/stdin
    # к дескрипторам консоли -- старые (заглушки) на них не указывают.
    sys.stdout = open('CONOUT$', 'w', buffering=1)
    sys.stderr = open('CONOUT$', 'w', buffering=1)
    sys.stdin = open('CONIN$', 'r')
    return allocated_new


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # argparse из коробки понимает -h/--help; остальные привычные варианты
    # запроса помощи добавляем сами.
    if argv and argv[0] in HELP_ALIASES:
        argv = ['--help']

    # Консоль нужна всем командам, кроме реального запуска monitor
    # (monitor + --help/-h -- это запрос помощи, ему консоль тоже нужна).
    is_monitor_run = (bool(argv) and argv[0] == 'monitor'
                       and '-h' not in argv and '--help' not in argv)
    allocated_new_console = False
    if not is_monitor_run:
        allocated_new_console = _ensure_console_for_cli()

    try:
        parser = _QuietArgumentParser(
            prog='ajazz_battery',
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub = parser.add_subparsers(dest='command', parser_class=_QuietArgumentParser)

        sub.add_parser('check', help='Разовая проверка заряда')
        sub.add_parser('sync-time', help='Разовая синхронизация времени докстанции')

        p_monitor = sub.add_parser('monitor', help='Фоновый монитор в трее')
        p_monitor.add_argument(
            '--interval', type=int, default=120, metavar='N',
            help='Интервал опроса заряда в секундах (по умолчанию 120)')
        p_monitor.add_argument(
            '--threshold', type=int, default=20, metavar='N',
            help='Порог уведомления о низком заряде, %% (по умолчанию 20)')
        p_monitor.add_argument(
            '--sync-interval', type=int, default=3600, metavar='N',
            help='Интервал синхронизации времени докстанции в секундах '
                 '(по умолчанию 3600 = раз в час)')

        args = parser.parse_args(argv)
        if args.command == 'monitor':
            if args.interval <= 0 or args.sync_interval <= 0:
                parser.error("Интервалы должны быть положительными")
            if not 0 <= args.threshold <= 100:
                parser.error("Порог должен быть в пределах 0-100")

        if args.command == 'check':
            cmd_check()
        elif args.command == 'sync-time':
            cmd_sync_time()
        elif args.command == 'monitor':
            cmd_monitor(args.interval, args.threshold, args.sync_interval)
        else:
            parser.print_help()
    finally:
        # Если сами создали новое окно консоли (двойной клик по exe) --
        # без паузы оно закроется мгновенно вместе с процессом, и вывод
        # прочитать не успеть. Выполняется даже при sys.exit() из парсера.
        if allocated_new_console:
            try:
                input("\nНажмите Enter для выхода...")
            except Exception:
                pass


if __name__ == '__main__':
    main()
