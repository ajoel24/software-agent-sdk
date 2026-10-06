import { HttpClient } from './http-client';
import type { TelegramChatSession, TelegramStartRequest, TelegramStatus } from '../telegram/types';

export interface TelegramClientOptions {
  host: string;
  apiKey?: string;
  timeout?: number;
}

export class TelegramClient {
  public readonly host: string;
  public readonly apiKey?: string;
  private readonly client: HttpClient;

  constructor(options: TelegramClientOptions) {
    this.host = options.host.replace(/\/$/, '');
    this.apiKey = options.apiKey;
    this.client = new HttpClient({
      baseUrl: this.host,
      apiKey: this.apiKey,
      timeout: options.timeout || 60000,
    });
  }

  async getStatus(): Promise<TelegramStatus> {
    const response = await this.client.get<TelegramStatus>('/telegram/status');
    return response.data;
  }

  async start(request: TelegramStartRequest = {}): Promise<{ status: string }> {
    const response = await this.client.post<{ status: string }>('/telegram/start', request);
    return response.data;
  }

  async stop(): Promise<{ status: string }> {
    const response = await this.client.post<{ status: string }>('/telegram/stop');
    return response.data;
  }

  async listChats(): Promise<TelegramChatSession[]> {
    const response = await this.client.get<TelegramChatSession[]>('/telegram/chats');
    return response.data;
  }

  close(): void {
    this.client.close();
  }
}
