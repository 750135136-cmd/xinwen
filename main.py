import re
import copy
import asyncio
import os
import cv2
import numpy as np
from datetime import datetime
from aip import AipOcr  # 需安装 baidu-aip
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityBold, MessageService
from telethon.extensions import html as tl_html

# ========= 配置 =========
API_ID = 25559912
API_HASH = "22d3bb9665ad7e6a86e89c1445672e07"
SESSION = "session"
SOURCE = "@ll111"
TARGET = "@hrxxw"
RESTART_TIME = 72000 
TAIL_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# ========= 百度 OCR 配置 (请替换为你的真实信息) =========
BAIDU_APP_ID = '122761270'
BAIDU_API_KEY = 'kVCUjj7y81g5WRit6dt8CozM'
BAIDU_SECRET_KEY = 'ZQ8dY4p2cj4ktwQcg7aHwQmkFzItK8eQ'
KEYWORD = "LL111"

ocr_client = AipOcr(BAIDU_APP_ID, BAIDU_API_KEY, BAIDU_SECRET_KEY)

client = TelegramClient(SESSION, API_ID, API_HASH)
SOURCE_ENTITY = None
TARGET_ENTITY = None

# ========= 日志 =========
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ========= OCR 逻辑 =========
def perform_ocr(image_bytes):
    """调用百度OCR识别文字，并增加错误捕获"""
    try:
        options = {"language_type": "CHN_ENG"}
        result = ocr_client.basicGeneral(image_bytes, options)
        
        # 如果有识别结果
        if "words_result" in result:
            text = "".join([w["words"] for w in result["words_result"]])
            return text
        
        # 如果返回了错误码（比如次数用完）
        if "error_code" in result:
            log(f"⚠️ OCR 接口报错: {result.get('error_msg')} (错误码: {result.get('error_code')})")
            # 次数用完的错误码通常是 17 (Open api daily request limit reached)
            # 或者 18 (Open api qps limit reached)
            
    except Exception as e:
        log(f"❌ OCR 请求发生物理异常: {e}")
    return ""


async def ocr_check_media(msg) -> bool:
    """
    检查媒体文件中是否包含关键字
    返回 True 表示命中关键字（拦截），False 表示未命中（通过）
    """
    if not msg or not msg.media:
        return False

    temp_path = await msg.download_media(file="temp_media")
    hit = False
    
    try:
        # 处理图片
        if msg.photo or (msg.document and "image" in (msg.document.mime_type or "")):
            with open(temp_path, 'rb') as f:
                img_data = f.read()
                detected_text = perform_ocr(img_data)
                if KEYWORD in detected_text:
                    hit = True

        # 处理视频
        elif msg.video or (msg.document and "video" in (msg.document.mime_type or "")):
            cap = cv2.VideoCapture(temp_path)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames > 0:
                # 抽帧10次
                for i in range(10):
                    frame_idx = int((total_frames / 11) * (i + 1))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                    success, frame = cap.read()
                    if success:
                        # 转换帧为图片字节
                        _, buffer = cv2.imencode('.jpg', frame)
                        detected_text = perform_ocr(buffer.tobytes())
                        if KEYWORD in detected_text:
                            hit = True
                            break
            cap.release()

    except Exception as e:
        log(f"媒体解析错误: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    result_str = "命中关键词" if hit else "未命中"
    log(f"识图结果: {result_str}")
    return hit

# ========= 基础判断 (保持不变) =========
def has_link(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"(https?://|www\.)", text, re.IGNORECASE))

def has_paid_ad(text: str) -> bool:
    return bool(text and "付费广告" in text)

def count_buttons(msg) -> int:
    if not getattr(msg, "buttons", None):
        return 0
    return sum(len(row) for row in msg.buttons)

def pick_text_from_message(msg):
    txt = getattr(msg, "raw_text", None) or getattr(msg, "message", None) or ""
    return txt, getattr(msg, "entities", None)

def pick_caption_from_album(event):
    if not event.messages:
        return "", []
    main_msg = event.messages[0]
    txt = getattr(main_msg, "raw_text", None) or getattr(main_msg, "message", None) or ""
    entities = getattr(main_msg, "entities", None) or []
    if not txt.strip():
        for m in event.messages[1:]:
            t = getattr(m, "raw_text", None) or getattr(m, "message", None) or ""
            if t.strip():
                txt = t
                entities = getattr(m, "entities", None) or []
                break
    return txt, list(entities or [])

def trim_tail_keep_entities(text: str, entities=None):
    if not text:
        return text, []
    raw = text.rstrip()
    entities = list(entities or [])
    matches = list(re.finditer(r"\n\s*\n", raw))
    if not matches:
        return raw, entities
    last_sep = matches[-1]
    prefix = raw[:last_sep.start()].rstrip()
    tail = raw[last_sep.end():].strip()
    if not tail or ("@" not in tail and not has_link(tail)):
        return raw, entities
    new_text = prefix + "\n\n" + TAIL_TEXT
    prefix_len = len(prefix)
    new_entities = []
    for ent in entities:
        start = ent.offset
        end = ent.offset + ent.length
        if end <= prefix_len:
            new_entities.append(copy.copy(ent))
            continue
        if start >= prefix_len:
            continue
        ent2 = copy.copy(ent)
        ent2.length = max(0, prefix_len - start)
        if ent2.length > 0:
            new_entities.append(ent2)
    new_entities.append(MessageEntityBold(offset=len(prefix) + 2, length=len(TAIL_TEXT)))
    return new_text, new_entities

def to_html(text: str, entities):
    if not text:
        return ""
    try:
        return tl_html.unparse(text, entities or [])
    except Exception:
        return text

# ========= 发送封装 (保持不变) =========
async def safe_send_single(*, target, text_html, media):
    try:
        await client.send_file(
            target,
            file=media,
            caption=text_html,
            parse_mode="html",
            link_preview=False,
        )
    except FloodWaitError as e:
        log(f"触发 FloodWait：需要等待 {e.seconds} 秒")
        if e.seconds > 60:
            log("等待时间过长，退出进程，交由 Railway 自动重启")
            await client.disconnect()
            raise SystemExit(1)
        await asyncio.sleep(e.seconds)
        await client.send_file(
            target,
            file=media,
            caption=text_html,
            parse_mode="html",
            link_preview=False,
        )

async def safe_send_album(*, target, files, captions_html):
    try:
        await client.send_file(
            target,
            file=files,
            caption=captions_html,
            parse_mode="html",
            link_preview=False,
        )
    except FloodWaitError as e:
        log(f"触发 FloodWait：需要等待 {e.seconds} 秒")
        if e.seconds > 60:
            log("等待时间过长，退出进程，交由 Railway 自动重启")
            await client.disconnect()
            raise SystemExit(1)
        await asyncio.sleep(e.seconds)
        await client.send_file(
            target,
            file=files,
            caption=captions_html,
            parse_mode="html",
            link_preview=False,
        )

# ========= 单条处理 (集成 OCR) =========
async def message_handler(event):
    try:
        if event.grouped_id:
            return
        msg = event.message
        text, entities = pick_text_from_message(msg)
        btn_count = count_buttons(msg)
        log(f"收到消息 | 文本长度:{len(text)} | 按钮:{btn_count} | 有媒体:{bool(msg.media)}")
        
        if not msg.media:
            log("拦截: 无媒体")
            return

        # --- OCR 检查 ---
        if await ocr_check_media(msg):
            log("拦截: 媒体识图命中关键词")
            return
        
        if not text.strip():
            log("拦截: 无文字")
            return
        if has_paid_ad(text):
            log("拦截: 含付费广告")
            return
        if 1 <= btn_count <= 3:
            log(f"拦截: 按钮数量 {btn_count}（1~3 禁止）")
            return
        new_text, new_entities = trim_tail_keep_entities(text, entities)
        if has_link(new_text):
            log("拦截: 尾部处理后仍包含链接")
            return
        text_html = to_html(new_text, new_entities)
        await safe_send_single(
            target=TARGET_ENTITY,
            text_html=text_html,
            media=msg.media
        )
        log("转发成功: 单条消息")
    except Exception as e:
        log(f"消息处理错误: {e}")

# ========= 相册处理 (集成 OCR) =========
async def album_handler(event):
    try:
        msgs = event.messages
        first = msgs[0]
        btn_count = count_buttons(first)
        text, entities = pick_caption_from_album(event)
        log(f"收到相册 | 媒体数:{len(msgs)} | 文本长度:{len(text)} | 按钮:{btn_count}")

        # --- OCR 检查 (遍历相册内所有媒体) ---
        for i, m in enumerate(msgs):
            log(f"正在识别相册第 {i+1} 个媒体...")
            if await ocr_check_media(m):
                log(f"拦截: 相册第 {i+1} 个媒体识图命中关键词")
                return

        if not text.strip():
            log("拦截: 相册无文字")
            return
        if has_paid_ad(text):
            log("拦截: 含付费广告")
            return
        if 1 <= btn_count <= 3:
            log(f"拦截: 按钮数量 {btn_count}（1~3 禁止）")
            return
        new_text, new_entities = trim_tail_keep_entities(text, entities)
        if has_link(new_text):
            log("拦截: 尾部处理后仍包含链接")
            return
        first_caption_html = to_html(new_text, new_entities)
        captions_html = [first_caption_html] + [""] * (len(msgs) - 1)
        await safe_send_album(
            target=TARGET_ENTITY,
            files=[m.media for m in msgs],
            captions_html=captions_html
        )
        log(f"转发成功: 相册 | 实际媒体数:{len(msgs)}")
    except Exception as e:
        log(f"相册处理错误: {e}")

# ========= 启动及主程序 (保持不变) =========
async def auto_restart():
    await asyncio.sleep(RESTART_TIME)
    log("20小时到，执行自动重启")
    await client.disconnect()
    raise SystemExit(0)

async def resolve_and_bind():
    global SOURCE_ENTITY, TARGET_ENTITY
    SOURCE_ENTITY = await client.get_entity(SOURCE)
    TARGET_ENTITY = await client.get_entity(TARGET)
    me = await client.get_me()
    log(f"启动成功 | 登录账号: {me.username or me.id}")
    client.add_event_handler(album_handler, events.Album(chats=SOURCE_ENTITY))
    client.add_event_handler(message_handler, events.NewMessage(chats=SOURCE_ENTITY))

async def main():
    await client.start()
    await resolve_and_bind()
    asyncio.create_task(auto_restart())
    await client.run_until_disconnected()

client.loop.run_until_complete(main())
