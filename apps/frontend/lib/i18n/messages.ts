import type { Locale } from '@/i18n/config';

import zh from '@/messages/zh.json';

export type Messages = typeof zh;

const allMessages: Record<Locale, Messages> = {
  zh,
};

export function getMessages(locale: Locale): Messages {
  return allMessages[locale] || allMessages.zh;
}
