import re
import copy
import asyncio
import io
import cv2
import numpy as np
from datetime import datetime
from PIL import Image
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityBold
from telethon.extensions import html as tl_html
# 新增：免费离线OCR库
import easyocr

# ========= 配置 =========
API_ID = 25559912
API_HASH = "22d3bb9665ad7e6a86e89c1445672e07"
SESSION = "session"   # 根目录下的 session.session
SOURCE = "@ll111"
TARGET = "@hrxxw"
RESTART_TIME = 72000  # 20小时
TAIL_TEXT = "关注华人新闻: @hrxxw 投稿: @LimTGbot"

# ========= 新增：OCR识别配置（免费离线，无API调用成本） =========
# 全局仅初始化1次OCR模型，避免重复加载占用内存/CPU
OCR_READER = easyocr.Reader(['en'], gpu=False, verbose=False)
# 识别目标关键词（大小写不敏感）
TARGET_KEYWORD = "LL111"
# 视频固定抽帧数量
VIDEO_SAMPLE_COUNT = 10

client = TelegramClient(SESSION, API_ID, API_HASH)
SOURCE_ENTITY = None
TARGET_ENTITY = None

# ========= 日志 =========
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ========= 新增：OCR识别核心函数 =========
def ocr_image_has_keyword(image_data: bytes) -> bool:
    """
    单张图片OCR识别，判断是否包含目标关键词
    :param image_data: 图片字节流
    :return: True=命中关键词，False=未命中
    """
    try:
        # 字节流转为OCR兼容的图片格式
        image = Image.open(io.BytesIO(image_data)).convert('RGB')
        image_np = np.array(image)
        # 识别文本（仅返回文本内容，关闭详情提升速度）
        result_texts = OCR_READER.readtext(image_np, detail=0, paragraph=True)
        # 合并文本并转为大写，实现大小写不敏感匹配
        full_text = " ".join(result_texts).upper()
        return TARGET_KEYWORD.upper() in full_text
    except Exception as e:
        log(f"OCR图片识别异常: {e}")
        # 异常时默认未命中，避免误拦截正常消息
        return False

async def media_ocr_check(media) -> tuple[bool, str]:
    """
    检查Telegram媒体（图片/视频）是否包含目标关键词
    :param media: Telethon的MessageMedia对象
    :return: (是否命中, 媒体类型)
    """
    # 识别媒体类型
    media_type = "未知媒体"
    is_image = False
    is_video = False

    # 兼容Telethon各类媒体封装格式
    if hasattr(media, 'photo') and media.photo:
        media_type = "图片"
        is_image = True
    elif hasattr(media, 'document') and media.document:
        mime_type = getattr(media.document, 'mime_type', '')
        if mime_type.startswith('video/'):
            media_type = "视频"
            is_video = True
        elif mime_type.startswith('image/'):
            media_type = "图片"
            is_image = True

    # 非图片/视频媒体直接放行，不拦截
    if not is_image and not is_video:
        return False, media_type

    try:
        # 下载媒体到内存（不写磁盘，适配Railway无状态环境）
        media_bytes = await client.download_media(media, file=io.BytesIO())
        media_bytes.seek(0)
        media_data = media_bytes.read()

        # 图片识别逻辑
        if is_image:
            is_hit = ocr_image_has_keyword(media_data)
            return is_hit, media_type

        # 视频识别逻辑：先识别封面，再10次均匀抽帧，命中即停止
        if is_video:
            import tempfile
            # 临时文件处理视频（OpenCV不支持直接读取内存视频流）
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=True) as temp_video:
                temp_video.write(media_data)
                temp_video.flush()

                # 打开视频文件
                cap = cv2.VideoCapture(temp_video.name)
                if not cap.isOpened():
                    log(f"视频打开失败，跳过识别")
                    return False, media_type

                # 第一步：识别视频封面（第0帧），命中直接返回
                ret, cover_frame = cap.read()
                if ret:
                    is_success, buffer = cv2.imencode(".jpg", cover_frame)
                    if is_success and ocr_image_has_keyword(buffer.tobytes()):
                        cap.release()
                        return True, media_type

                # 第二步：均匀抽取10帧识别，命中即停止
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                if total_frames <= 0:
                    cap.release()
                    return False, media_type

                # 计算抽帧间隔，避免重复/无效抽帧
                sample_interval = max(total_frames // VIDEO_SAMPLE_COUNT, 1)
                hit_result = False

                for i in range(VIDEO_SAMPLE_COUNT):
                    frame_num = min((i + 1) * sample_interval, total_frames - 1)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                    ret, frame = cap.read()
                    if not ret:
                        continue

                    # 识别当前帧
                    is_success, buffer = cv2.imencode(".jpg", frame)
                    if not is_success:
                        continue

                    if ocr_image_has_keyword(buffer.tobytes()):
                        hit_result = True
                        break

                # 释放视频资源
                cap.release()
                return hit_result, media_type

    except Exception as e:
        log(f"媒体OCR检查异常: {e}")
        # 异常时默认放行，避免误拦截
        return False, media_type

# ========= 基础判断 =========
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

# ========= 【修复】相册文本&实体获取：完全兼容Telethon 1.42.0，正确保留所有格式实体 =========
def pick_caption_from_album(event):
    # Telethon 1.42.0 相册标准行为：caption和对应格式实体固定在第一条消息中
    if not event.messages:
        return "", []
    main_msg = event.messages[0]
    # 优先从主消息获取文本和实体，和单条消息逻辑完全对齐
    txt = getattr(main_msg, "raw_text", None) or getattr(main_msg, "message", None) or ""
    entities = getattr(main_msg, "entities", None) or []
    
    # 兜底兼容：主消息无文本时，遍历所有消息找有效文本和对应实体
    if not txt.strip():
        for m in event.messages[1:]:
            t = getattr(m, "raw_text", None) or getattr(m, "message", None) or ""
            if t.strip():
                txt = t
                entities = getattr(m, "entities", None) or []
                break
    
    # 最终兜底：确保实体永远是列表，不会出现None，避免格式处理失效
    return txt, list(entities or [])

# ========= 尾部处理：只改最后一段 =========
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
    """
    把 text + entities 转成 HTML，
    这样发送时不会退回成 * 号，也更稳地保留引用框/加粗。
    """
    if not text:
        return ""
    try:
        return tl_html.unparse(text, entities or [])
    except Exception:
        # 兜底：至少保证不会因为格式转换崩掉
        return text

# ========= 发送封装 =========
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

# ========= 【修复】相册发送：删除重复拼接caption的错误逻辑，保证长度和媒体数完全匹配 =========
async def safe_send_album(*, target, files, captions_html):
    """
    相册/多媒体组：
    每个文件都使用 HTML 格式的 caption（确保每个文件单独传递 HTML 格式）。
    """
    try:
        # 修复：直接使用传入的captions_html，已保证长度和files完全一致，无需重复拼接
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
        # 重试时也直接使用原captions_html，保证长度匹配
        await client.send_file(
            target,
            file=files,
            caption=captions_html,
            parse_mode="html",
            link_preview=False,
        )

# ========= 单条处理 =========
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
        
        # ========= 新增：媒体OCR关键词拦截检查 =========
        is_hit, media_type = await media_ocr_check(msg.media)
        if is_hit:
            log(f"拦截: {media_type}命中关键词{TARGET_KEYWORD}，不发送")
            return
        log(f"{media_type}未命中关键词{TARGET_KEYWORD}，继续转发流程")
        # ========= 新增结束 =========

        text_html = to_html(new_text, new_entities)
        await safe_send_single(
            target=TARGET_ENTITY,
            text_html=text_html,
            media=msg.media
        )
        log("转发成功: 单条消息")
    except Exception as e:
        log(f"消息处理错误: {e}")

# ========= 相册处理 =========
async def album_handler(event):
    try:
        msgs = event.messages
        first = msgs[0]
        btn_count = count_buttons(first)
        # 修复后：正确获取相册文本和完整格式实体，和单条消息处理逻辑完全对齐
        text, entities = pick_caption_from_album(event)
        log(f"收到相册 | 媒体数:{len(msgs)} | 文本长度:{len(text)} | 按钮:{btn_count}")
        if not text.strip():
            log("拦截: 相册无文字")
            return
        if has_paid_ad(text):
            log("拦截: 含付费广告")
            return
        if 1 <= btn_count <= 3:
            log(f"拦截: 按钮数量 {btn_count}（1~3 禁止）")
            return
        # 完全复用单条消息的尾部处理&格式转换逻辑，保证一致性
        new_text, new_entities = trim_tail_keep_entities(text, entities)
        if has_link(new_text):
            log("拦截: 尾部处理后仍包含链接")
            return
        
        # ========= 新增：相册全媒体OCR关键词拦截检查 =========
        all_media = [m.media for m in msgs]
        for idx, media in enumerate(all_media):
            is_hit, media_type = await media_ocr_check(media)
            if is_hit:
                log(f"拦截: 相册第{idx+1}个{media_type}命中关键词{TARGET_KEYWORD}，整个相册不发送")
                return
        log(f"相册所有媒体未命中关键词{TARGET_KEYWORD}，继续转发流程")
        # ========= 新增结束 =========

        # 转换为 HTML 格式，完整保留加粗、引用框等所有格式
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

# ========= 20小时自动重启 =========
async def auto_restart():
    await asyncio.sleep(RESTART_TIME)
    log("20小时到，执行自动重启")
    await client.disconnect()
    raise SystemExit(0)

# ========= 启动前解析实体并绑定监听 =========
async def resolve_and_bind():
    global SOURCE_ENTITY, TARGET_ENTITY
    SOURCE_ENTITY = await client.get_entity(SOURCE)
    TARGET_ENTITY = await client.get_entity(TARGET)
    me = await client.get_me()
    log(f"启动成功 | 登录账号: {me.username or me.id}")
    log(f"监听频道: {getattr(SOURCE_ENTITY, 'title', '')} | username: @{getattr(SOURCE_ENTITY, 'username', None) or '无公开用户名'}")
    log(f"目标频道: {getattr(TARGET_ENTITY, 'title', '')} | username: @{getattr(TARGET_ENTITY, 'username', None) or '无公开用户名'}")
    client.add_event_handler(album_handler, events.Album(chats=SOURCE_ENTITY))
    client.add_event_handler(message_handler, events.NewMessage(chats=SOURCE_ENTITY))

# ========= 主程序 =========
async def main():
    await client.start()
    await resolve_and_bind()
    asyncio.create_task(auto_restart())
    await client.run_until_disconnected()

client.loop.run_until_complete(main())
