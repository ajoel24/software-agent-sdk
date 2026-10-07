/** Telegram bot integration types (mirrors the agent-server router models). */

export type TelegramBotStatus = 'stopped' | 'starting' | 'running' | 'error';

export interface TelegramStartRequest {
  bot_token?: string;
  webhook_url?: string;
  allowed_usernames?: string[];
  default_workspace?: string;
}

export interface TelegramStatus {
  status: TelegramBotStatus;
  active_chats: number;
  total_messages: number;
  bot_token_configured: boolean;
  webhook_url?: string;
}

export interface TelegramChatSession {
  chat_id: number;
  chat_title?: string;
  chat_username?: string;
  conversation_id?: string;
  status: string;
  message_count: number;
  last_activity: string;
}
