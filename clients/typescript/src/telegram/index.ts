/**
 * Telegram bot integration client.
 *
 * Provides typed methods for the agent-server `/telegram/*` endpoints.
 * Used by Agent Canvas UI to manage the Telegram bot.
 */

export type {
  TelegramBotStatus,
  TelegramStartRequest,
  TelegramStatus,
  TelegramChatSession,
} from './types';

import { TELEGRAM_ROUTES } from './routes';
import type { TelegramChatSession, TelegramStartRequest, TelegramStatus } from './types';

export interface TelegramClientOptions {
  baseUrl: string;
  headers?: Record<string, string>;
}

async function request<T>(
  options: TelegramClientOptions,
  path: string,
  init?: RequestInit
): Promise<T> {
  const response = await fetch(`${options.baseUrl}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
      ...init?.headers,
    },
  });
  if (!response.ok) {
    throw new Error(`Telegram API error: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function getTelegramStatus(options: TelegramClientOptions): Promise<TelegramStatus> {
  return request<TelegramStatus>(options, TELEGRAM_ROUTES.status);
}

export async function startTelegramBot(
  options: TelegramClientOptions,
  req: TelegramStartRequest
): Promise<{ status: string }> {
  return request(options, TELEGRAM_ROUTES.start, {
    method: 'POST',
    body: JSON.stringify(req),
  });
}

export async function stopTelegramBot(options: TelegramClientOptions): Promise<{ status: string }> {
  return request(options, TELEGRAM_ROUTES.stop, { method: 'POST' });
}

export async function listTelegramChats(
  options: TelegramClientOptions
): Promise<TelegramChatSession[]> {
  return request<TelegramChatSession[]>(options, TELEGRAM_ROUTES.chats);
}
