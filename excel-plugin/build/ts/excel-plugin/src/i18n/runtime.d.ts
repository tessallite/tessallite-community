type StringTree = {
    readonly [key: string]: string | StringTree;
};
export declare function normaliseLocale(locale?: string | null): string;
export declare function setActiveLocale(locale?: string | null): string;
export declare function getActiveLocale(): string;
export declare function initialiseLocale(): string;
export declare function localizeObject<T extends StringTree>(english: T): T;
export declare function translateChat(key: string, english: string, params?: Record<string, string | number>): string;
export {};
