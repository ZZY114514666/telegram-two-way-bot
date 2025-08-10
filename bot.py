#!/usr/bin/env python3
# coding: utf-8
"""
Telegram 双向中转机器人（全按钮版）
- 普通用户只能看到“申请与管理员连接 / 取消申请 / 结束聊天”按钮
- 管理员可以查看申请、同意/拒绝、主动连接、结束会话、查看活动列表
- 多用户同时在线；消息复制（copy）发送，非转发
- 支持文本/图片/文件/语音/视频/贴纸等（通过 Message.copy）
"""

import os
import logging
from typing import Dict, Set, Optional

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
if not BOT_TOKEN or ADMIN_ID == 0:
    raise RuntimeError("请设置环境变量 BOT_TOKEN 和 ADMIN_ID（数字）")

# ---------- LOG ----------
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- IN-MEMORY DATA ----------
pending_requests: Set[int] = set()           # user_id set waiting admin approval
active_sessions: Set[int] = set()            # user_id set currently connected
# mapping: message_id (admin side) -> user_id (so admin reply_to_message can be routed)
admin_msgid_to_user: Dict[int, int] = {}
# mapping: user_id -> last admin-side message id (for convenience)
user_last_admin_msgid: Dict[int, int] = {}

# ---------- KEYBOARDS ----------
def user_main_keyboard(is_pending: bool, is_active: bool):
    """用户看到的主键盘：申请 / 取消申请 / 结束聊天"""
    if is_active:
        kb = [[InlineKeyboardButton("🔚 结束聊天", callback_data="user_end")]]
    elif is_pending:
        kb = [[InlineKeyboardButton("⏳ 取消申请", callback_data="user_cancel")]]
    else:
        kb = [[InlineKeyboardButton("📨 申请与管理员连接", callback_data="user_apply")]]
    return InlineKeyboardMarkup(kb)

def admin_panel_keyboard():
    """管理员面板入口键盘"""
    kb = [
        [
            InlineKeyboardButton("📥 查看申请", callback_data="admin_view_pending"),
            InlineKeyboardButton("📋 活动会话", callback_data="admin_view_active"),
        ],
        [InlineKeyboardButton("📤 主动连接用户（命令）", callback_data="admin_hint_connect")],
    ]
    return InlineKeyboardMarkup(kb)

def pending_item_kb(user_id: int):
    kb = [
        [
            InlineKeyboardButton("✅ 同意", callback_data=f"admin_accept:{user_id}"),
            InlineKeyboardButton("❌ 拒绝", callback_data=f"admin_reject:{user_id}"),
        ]
    ]
    return InlineKeyboardMarkup(kb)

def active_item_kb(user_id: int):
    kb = [
        [InlineKeyboardButton("🔚 结束该会话", callback_data=f"admin_end:{user_id}")],
    ]
    return InlineKeyboardMarkup(kb)

# ---------- HELPERS ----------
async def send_admin_panel(update_or_ctx, context: ContextTypes.DEFAULT_TYPE):
    """发送管理员主面板（用于 /start 或按钮）"""
    if isinstance(update_or_ctx, ContextTypes.DEFAULT_TYPE):
        ctx = update_or_ctx
        chat_id = ADMIN_ID
    else:
        ctx = context
        chat_id = update_or_ctx.effective_chat.id

    text = "管理面板：\n- 查看申请并同意/拒绝\n- 查看活动会话并结束\n\n说明：管理员可用 /connect <user_id> 主动连接（无需用户申请）"
    await ctx.bot.send_message(chat_id=chat_id, text=text, reply_markup=admin_panel_keyboard())

async def notify_admin_of_request(user_id: int, username: Optional[str], context: ContextTypes.DEFAULT_TYPE):
    """当用户申请时，给管理员发送一条带按钮的申请消息"""
    name = f"@{username}" if username else str(user_id)
    text = f"📌 新请求：用户 {name}\nID: `{user_id}`\n是否同意？"
    sent = await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        reply_markup=pending_item_kb(user_id),
        parse_mode="Markdown"
    )
    # store mapping in case admin replies etc (optional)

# ---------- COMMANDS ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        # admin: show full panel
        await update.message.reply_text("欢迎，管理员。", reply_markup=admin_panel_keyboard())
    else:
        # user: only show apply button
        is_pending = uid in pending_requests
        is_active = uid in active_sessions
        await update.message.reply_text(
            "欢迎。点击下面按钮向管理员申请聊天。",
            reply_markup=user_main_keyboard(is_pending=is_pending, is_active=is_active)
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        txt = (
            "/start - 管理面板\n"
            "/connect <user_id> - 管理员主动和某用户建立会话（无需用户申请）\n"
            "/panel - 刷新管理面板\n"
        )
        await update.message.reply_text(txt)
    else:
        await update.message.reply_text("使用 /start 按钮申请与管理员连接。")

async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await send_admin_panel(update, context)

async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员主动连接： /connect <user_id>"""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("用法：/connect <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 必须是数字")
        return

    # establish session immediately
    pending_requests.discard(uid)
    active_sessions.add(uid)
    # notify both
    await update.message.reply_text(f"✅ 已主动与用户 {uid} 建立会话（无需申请）。")
    try:
        await context.bot.send_message(chat_id=uid, text="✅ 管理员已与你建立专属聊天通道（管理员主动发起）。")
    except Exception as e:
        await update.message.reply_text(f"注意：向用户发送消息失败：{e}")

# ---------- CALLBACK QUERY HANDLER ----------
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有按钮回调"""
    query = update.callback_query
    await query.answer()  # 避免转圈
    data = query.data
    user = query.from_user

    # --- 用户侧按钮 ---
    if data == "user_apply":
        uid = user.id
        if uid in active_sessions:
            await query.edit_message_text("你已处于会话中；点“结束聊天”来断开。")
            return
        if uid in pending_requests:
            await query.edit_message_text("你已申请，请等待管理员处理。", reply_markup=user_main_keyboard(is_pending=True, is_active=False))
            return
        pending_requests.add(uid)
        # notify user (update their message buttons)
        await query.edit_message_text("✅ 已发送申请，请耐心等待管理员确认。", reply_markup=user_main_keyboard(is_pending=True, is_active=False))
        # notify admin
        await notify_admin_of_request(uid, user.username, context)
        return

    if data == "user_cancel":
        uid = user.id
        if uid in pending_requests:
            pending_requests.discard(uid)
            await query.edit_message_text("已取消申请。", reply_markup=user_main_keyboard(is_pending=False, is_active=False))
            # optionally notify admin of cancellation
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"ℹ️ 用户 `{uid}` 取消了申请。", parse_mode="Markdown")
            except:
                pass
        else:
            await query.edit_message_text("你当前没有申请。", reply_markup=user_main_keyboard(is_pending=False, is_active=False))
        return

    if data == "user_end":
        uid = user.id
        if uid in active_sessions:
            active_sessions.discard(uid)
            # notify admin
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ 用户 `{uid}` 已结束会话。", parse_mode="Markdown")
            except:
                pass
            await query.edit_message_text("你已结束与管理员的会话。", reply_markup=user_main_keyboard(is_pending=False, is_active=False))
        else:
            await query.edit_message_text("你当前没有会话。", reply_markup=user_main_keyboard(is_pending=False, is_active=False))
        return

    # --- 管理端按钮 ---
    if data == "admin_view_pending":
        # list pending requests
        if not pending_requests:
            await query.edit_message_text("当前没有待处理的申请。", reply_markup=admin_panel_keyboard())
            return
        # send each pending as a separate message with accept/reject
        text = "待处理申请："
        await query.edit_message_text(text, reply_markup=admin_panel_keyboard())
        for uid in list(pending_requests):
            name = f"@{uid}"  # username not stored; admin can see id
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"📌 申请用户 ID: `{uid}`",
                    reply_markup=pending_item_kb(uid),
                    parse_mode="Markdown"
                )
            except Exception:
                logger.exception("notify admin failed")
        return

    if data == "admin_view_active":
        if not active_sessions:
            await query.edit_message_text("当前没有活动会话。", reply_markup=admin_panel_keyboard())
            return
        # show active sessions with end buttons
        await query.edit_message_text("活动会话列表：", reply_markup=admin_panel_keyboard())
        for uid in list(active_sessions):
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🟢 活动用户 ID: `{uid}`",
                    reply_markup=active_item_kb(uid),
                    parse_mode="Markdown"
                )
            except Exception:
                logger.exception("sending active list item failed")
        return

    if data.startswith("admin_accept:"):
        parts = data.split(":")
        try:
            uid = int(parts[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in pending_requests:
            pending_requests.discard(uid)
            active_sessions.add(uid)
            # notify both
            await query.edit_message_text(f"✅ 已同意用户 `{uid}` 的申请。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="✅ 管理员已同意你的申请，你现在已连接到管理员。")
            except Exception:
                pass
            # notify admin there's an active session message
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"🟢 与用户 `{uid}` 建立连接。", parse_mode="Markdown")
        else:
            await query.edit_message_text("该用户不在申请队列或已被处理。")
        return

    if data.startswith("admin_reject:"):
        parts = data.split(":")
        try:
            uid = int(parts[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in pending_requests:
            pending_requests.discard(uid)
            await query.edit_message_text(f"❌ 已拒绝用户 `{uid}` 的申请。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="很抱歉，管理员拒绝了你的聊天申请。")
            except:
                pass
        else:
            await query.edit_message_text("该用户不在申请队列或已被处理。")
        return

    if data.startswith("admin_end:"):
        parts = data.split(":")
        try:
            uid = int(parts[1])
        except:
            await query.edit_message_text("ID 格式错误")
            return
        if uid in active_sessions:
            active_sessions.discard(uid)
            await query.edit_message_text(f"🔚 已结束用户 `{uid}` 的会话。", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="⚠️ 管理员已结束本次会话。")
            except:
                pass
        else:
            await query.edit_message_text("该用户当前没有活动会话。")
        return

    if data == "admin_hint_connect":
        # show hint that admin should use /connect
        await query.edit_message_text("请使用命令：/connect <user_id> 来主动与某用户连接（无需用户申请）。", reply_markup=admin_panel_keyboard())
        return

    # default fallback
    await query.answer(text="未识别的操作。")

# ---------- MESSAGE RELAY ----------
async def message_relay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """所有非命令消息都到这里处理：
    - 用户发消息：如果 active -> 复制到 admin；如果 pending -> 提示等待；否则显示申请按钮
    - 管理员发消息：如果 reply_to_message 对应某 user -> 复制到该用户；否则提示使用回复或 /connect
    """
    msg: Message = update.effective_message
    sender_id = update.effective_user.id

    # ADMIN path
    if sender_id == ADMIN_ID:
        # If admin replies to a bot message that we previously forwarded (admin-side msg -> user mapping)
        reply = msg.reply_to_message
        if reply and reply.message_id in admin_msgid_to_user:
            target_user = admin_msgid_to_user[reply.message_id]
            try:
                copied = await msg.copy(chat_id=target_user)
                # optionally record last mapping
                user_last_admin_msgid[target_user] = copied.message_id
                # notify admin
                await msg.reply_text(f"已发送给用户 {target_user}")
            except Exception as e:
                logger.exception("admin -> user copy failed")
                await msg.reply_text(f"发送失败：{e}")
            return

        # Admin not replying to a forwarded user message: inform how to reply/use panel
        await msg.reply_text("要回复某个用户，请在管理面板查看活动会话并回复对应消息，或使用 /connect <user_id> 以主动连接。")
        return

    # USER path
    # If user is active: copy message to admin and record mapping
    if sender_id in active_sessions:
        try:
            copied = await msg.copy(chat_id=ADMIN_ID)
            # map the admin-side message id -> user id so admin reply_to_message can route
            admin_msgid_to_user[copied.message_id] = sender_id
            user_last_admin_msgid[sender_id] = copied.message_id
        except Exception:
            logger.exception("user -> admin copy failed")
            try:
                await msg.reply_text("发送失败，请稍后重试。")
            except:
                pass
        return

    # If pending: user waiting
    if sender_id in pending_requests:
        await msg.reply_text("⏳ 你的申请正在等待管理员处理，请耐心等待或点击取消。", reply_markup=user_main_keyboard(is_pending=True, is_active=False))
        return

    # otherwise user not applied: prompt apply button (user can click)
    await msg.reply_text("你当前尚未申请与管理员聊天。点击下面按钮申请：", reply_markup=user_main_keyboard(is_pending=False, is_active=False))
    return

# ---------- MAIN ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))
    app.add_handler(CommandHandler("connect", connect_cmd))  # admin only

    # callback queries (buttons)
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    # all other messages
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), message_relay_handler))

    logger.info("Bot started (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
