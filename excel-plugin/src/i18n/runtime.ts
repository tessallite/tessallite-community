type StringTree = { readonly [key: string]: string | StringTree };

const RTL_LOCALES = new Set(["ar"]);
let activeLocale = "en";

const taskPaneTranslations: Record<string, Record<string, string>> = {
  de: {
    "status.connected": "Verbunden",
    "status.disconnected": "Getrennt",
    "status.reconnecting": "Verbindung wird wiederhergestellt",
    "status.loading": "Wird geladen...",
    "app.askTessallite": "Tessallite fragen",
    "app.loadingProviderInfo": "Anbieterinformationen werden geladen...",
    "app.selectedValue": "Ausgewählter Wert",
    "app.drillThrough": "Drillthrough",
    "app.drillThroughCell": "Drillthrough für ausgewählte Zelle",
    "app.sectionsAria": "Tessallite-Taskpane-Bereiche",
    "app.projectSelectorAria": "Projektauswahl",
    "app.glossary": "Glossar",
    "app.glossaryAria": "Glossar öffnen",
    "app.settings": "Einstellungen",
    "app.settingsAria": "Einstellungen öffnen",
    "app.diagnosticsMenuItem": "Diagnose",
    "app.signOut": "Abmelden",
    "app.projectLabel": "Projekt",
    "app.tabAnalyse": "Analysieren",
    "app.tabKpis": "KPIs",
    "app.retry": "Erneut versuchen",
    "app.viewingAs": "Ansicht als",
    "app.resetToDefault": "Auf Standard zurücksetzen",
    "app.chatMessagesAria": "Chatnachrichten",
    "app.reportBuilderAria": "Berichtsgenerator",
    "app.kpiScorecardAria": "KPI-Scorecard",
    "app.profileRemoved": "Profil entfernt",
    "projects.none": "Keine Projekte verfügbar. Erstellen Sie eines in der Tessallite-Webanwendung.",
    "projects.noModels": "Keine Modelle für das ausgewählte Projekt verfügbar.",
    "projects.loadFailed": "Projekte konnten nicht geladen werden",
    "projects.noneSelected": "Kein Projekt ausgewählt. Fügen Sie ein Projekt in der Tessallite-Webanwendung hinzu.",
    "auth.loginFailed": "Anmeldung fehlgeschlagen",
    "connection.restored": "Verbindung wiederhergestellt",
    "connection.lost": "Verbindung verloren. Wiederholung läuft...",
    "chatShell.newConversation": "Neue Unterhaltung",
    "chatShell.conversationHistory": "Unterhaltungsverlauf",
    "chatShell.deleteTitle": "Unterhaltung löschen",
    "chatShell.deleteConfirmation": "Diese Aktion kann nicht rückgängig gemacht werden. Unterhaltung löschen?",
    "chatShell.cancel": "Abbrechen",
    "chatShell.delete": "Löschen",
    "chatShell.unavailableTitle": "Konversationsanalyse nicht verfügbar",
    "chatShell.unavailableDescription": "Bitten Sie Ihren Tessallite-Administrator, einen LLM-Anbieter zu konfigurieren.",
    "chatShell.composerPlaceholder": "Stellen Sie eine Frage zu Ihren Daten...",
    "errorBoundary.title": "Etwas ist schiefgelaufen",
    "errorBoundary.description": "Im Plugin ist ein unerwarteter Fehler aufgetreten. Schließen und öffnen Sie den Taskbereich erneut.",
    "errorBoundary.persistNote": "Wenn das Problem weiterhin besteht, wenden Sie sich an Ihren Tessallite-Administrator.",
  },
  fr: {
    "status.connected": "Connecté",
    "status.disconnected": "Déconnecté",
    "status.reconnecting": "Reconnexion",
    "status.loading": "Chargement...",
    "app.askTessallite": "Demander à Tessallite",
    "app.loadingProviderInfo": "Chargement des informations du fournisseur...",
    "app.selectedValue": "Valeur sélectionnée",
    "app.drillThrough": "Voir le détail",
    "app.drillThroughCell": "Voir le détail de la cellule sélectionnée",
    "app.sectionsAria": "Sections du volet Tessallite",
    "app.projectSelectorAria": "Sélecteur de projet",
    "app.glossary": "Glossaire",
    "app.glossaryAria": "Ouvrir le glossaire",
    "app.settings": "Paramètres",
    "app.settingsAria": "Ouvrir les paramètres",
    "app.diagnosticsMenuItem": "Diagnostics",
    "app.signOut": "Se déconnecter",
    "app.projectLabel": "Projet",
    "app.tabAnalyse": "Analyser",
    "app.tabKpis": "KPIs",
    "app.retry": "Réessayer",
    "app.viewingAs": "Vue en tant que",
    "app.resetToDefault": "Rétablir la valeur par défaut",
    "app.chatMessagesAria": "Messages du chat",
    "app.reportBuilderAria": "Générateur de rapports",
    "app.kpiScorecardAria": "Tableau de bord KPI",
    "app.profileRemoved": "Profil supprimé",
    "projects.none": "Aucun projet disponible. Créez-en un dans l'application web Tessallite.",
    "projects.noModels": "Aucun modèle disponible pour le projet sélectionné.",
    "projects.loadFailed": "Impossible de charger les projets",
    "projects.noneSelected": "Aucun projet sélectionné. Ajoutez un projet dans l'application web Tessallite.",
    "auth.loginFailed": "Échec de la connexion",
    "connection.restored": "Connexion rétablie",
    "connection.lost": "Connexion perdue. Nouvelle tentative...",
    "chatShell.newConversation": "Nouvelle conversation",
    "chatShell.conversationHistory": "Historique des conversations",
    "chatShell.deleteTitle": "Supprimer la conversation",
    "chatShell.deleteConfirmation": "Cette action est irréversible. Supprimer cette conversation ?",
    "chatShell.cancel": "Annuler",
    "chatShell.delete": "Supprimer",
    "chatShell.unavailableTitle": "Analyse conversationnelle indisponible",
    "chatShell.unavailableDescription": "Contactez votre administrateur Tessallite pour configurer un fournisseur LLM.",
    "chatShell.composerPlaceholder": "Posez une question sur vos données...",
    "errorBoundary.title": "Un problème est survenu",
    "errorBoundary.description": "Le plugin a rencontré une erreur inattendue. Fermez puis rouvrez le volet de tâches.",
    "errorBoundary.persistNote": "Si le problème persiste, contactez votre administrateur Tessallite.",
  },
};

const chatTranslations: Record<string, Record<string, string>> = {
  de: {
    "chat.chatAria": "Chat",
    "chat.emptyHint": "Stellen Sie eine Frage, um zu beginnen.",
    "chat.emptyTitle": "Noch keine Nachrichten",
    "chat.requestError": "Etwas ist schiefgelaufen. Bitte versuchen Sie es erneut.",
    "chat.retry": "Erneut versuchen",
    "chat.thinking": "Denkt nach...",
    "composer.placeholder": "Stellen Sie eine Frage zu Ihren Daten...",
    "composer.messageInputAria": "Nachrichteneingabe",
    "composer.sendAria": "Nachricht senden",
    "turn.errorFallback": "Beim Erstellen dieser Antwort ist ein Fehler aufgetreten.",
    "turn.refused": "Diese Anfrage wurde abgelehnt",
    "badges.rows": "{{count}} Zeilen",
    "badges.seconds": "{{seconds}} s",
    "chart.truncated": "Zeige die ersten {{count}} von {{total}} Zeilen",
    "steps.header": "Schritte ({{n}})",
    "steps.step": "Schritt {{n}}",
  },
  fr: {
    "chat.chatAria": "Chat",
    "chat.emptyHint": "Posez une question pour commencer.",
    "chat.emptyTitle": "Aucun message pour le moment",
    "chat.requestError": "Un problème est survenu. Réessayez.",
    "chat.retry": "Réessayer",
    "chat.thinking": "Réflexion...",
    "composer.placeholder": "Posez une question sur vos données...",
    "composer.messageInputAria": "Saisie du message",
    "composer.sendAria": "Envoyer le message",
    "turn.errorFallback": "Une erreur est survenue pendant la génération de cette réponse.",
    "turn.refused": "Cette demande a été refusée",
    "badges.rows": "{{count}} lignes",
    "badges.seconds": "{{seconds}} s",
    "chart.truncated": "Affichage des {{count}} premières lignes sur {{total}}",
    "steps.header": "Étapes ({{n}})",
    "steps.step": "Étape {{n}}",
  },
};

export function normaliseLocale(locale?: string | null): string {
  const base = (locale || "en").toLowerCase().split(/[-_]/)[0];
  return base || "en";
}

export function setActiveLocale(locale?: string | null): string {
  activeLocale = normaliseLocale(locale);
  if (typeof document !== "undefined") {
    document.documentElement.lang = activeLocale;
    document.documentElement.dir = RTL_LOCALES.has(activeLocale) ? "rtl" : "ltr";
  }
  return activeLocale;
}

export function getActiveLocale(): string {
  return activeLocale;
}

export function initialiseLocale(): string {
  const officeLocale = typeof Office !== "undefined"
    ? Office.context?.displayLanguage || Office.context?.contentLanguage
    : undefined;
  const browserLocale = typeof navigator !== "undefined" ? navigator.language : undefined;
  return setActiveLocale(officeLocale || browserLocale || "en");
}

function interpolate(template: string, params?: Record<string, string | number>): string {
  if (!params) return template;
  let rendered = template;
  for (const [key, value] of Object.entries(params)) {
    rendered = rendered.split(`{{${key}}}`).join(String(value));
  }
  return rendered;
}

function flatten(tree: StringTree, prefix = "", out: Record<string, string> = {}): Record<string, string> {
  for (const [key, value] of Object.entries(tree)) {
    const fullKey = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") out[fullKey] = value;
    else flatten(value, fullKey, out);
  }
  return out;
}

function valueAt(tree: StringTree, path: string[]): string | StringTree {
  let current: string | StringTree = tree;
  for (const segment of path) {
    if (typeof current === "string") return current;
    current = current[segment] as string | StringTree;
  }
  return current;
}

export function localizeObject<T extends StringTree>(english: T): T {
  const englishFlat = flatten(english);
  const wrap = (path: string[]): StringTree =>
    new Proxy({} as StringTree, {
      get(_target, prop) {
        if (typeof prop !== "string") return undefined;
        const nextPath = [...path, prop];
        const englishValue = valueAt(english, nextPath);
        if (typeof englishValue === "string") {
          const key = nextPath.join(".");
          return taskPaneTranslations[activeLocale]?.[key] ?? englishFlat[key] ?? englishValue;
        }
        return wrap(nextPath);
      },
    });
  return wrap([]) as T;
}

export function translateChat(
  key: string,
  english: string,
  params?: Record<string, string | number>,
): string {
  const template = chatTranslations[activeLocale]?.[key] ?? english;
  return interpolate(template, params);
}
