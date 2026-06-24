/**
 * Internationalization configuration
 */

export const locales = ['zh'] as const;
export type Locale = (typeof locales)[number];

export const defaultLocale: Locale = 'zh';

export const localeNames: Record<Locale, string> = {
  zh: '中文',
};

export const localeFlags: Record<Locale, string> = {
  zh: '🇨🇳',
};
