import { useState } from "react";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { useParams } from "react-router-dom";
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Button,
  Chip,
  IconButton,
  Link,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DownloadIcon from "@mui/icons-material/Download";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import HelpOutlineIcon from "@mui/icons-material/HelpOutline";
import CheckIcon from "@mui/icons-material/Check";
import { useModel, useProject } from "../../api/hooks";
import { getSystemDefaults } from "../../api/systemDefaults";
import { ui } from "../../theme/tokens";
import { useT } from "../../i18n";

// ---------------------------------------------------------------------------
// Env-configurable base URLs (fall back to local defaults)
// ---------------------------------------------------------------------------
function runtimeHost() {
  if (typeof window === "undefined") {
    return "localhost";
  }
  return window.location.hostname || "localhost";
}

function runtimeProtocol() {
  if (typeof window === "undefined") {
    return "http:";
  }
  return window.location.protocol || "http:";
}

function runtimeSetting(storageKey: string, envValue: string | undefined, fallback: string) {
  const fromStorage =
    typeof window !== "undefined" ? safeLocalGet(storageKey, "") : "";
  if (fromStorage.trim()) {
    return fromStorage.trim();
  }
  return envValue ?? fallback;
}

// A non-local host means we are running on a real deployment. On such
// deployments the gateway lives on its own subdomain (e.g.
// sql.cloud.tessallite.io), reachable only when the build baked in
// VITE_GATEWAY_URL / VITE_GATEWAY_JDBC_HOST. If those build-args are absent,
// the host-based fallback resolves to the APP domain — where the gateway does
// NOT listen — so the connection strings would point users at a dead address
// (Bug-5540). We detect that case and surface a warning instead of silently
// advertising the wrong host.
function isLocalHost(host: string): boolean {
  return (
    host === "localhost" ||
    host === "127.0.0.1" ||
    host === "0.0.0.0" ||
    host === "::1" ||
    host === "[::1]" ||
    host.endsWith(".local") ||
    host.endsWith(".localhost")
  );
}

interface ResolvedEndpoint {
  value: string;
  // false when the value is a host-based fallback on a non-local deployment,
  // i.e. the gateway address could not be reliably determined.
  reliable: boolean;
}

// Resolves a gateway endpoint, tracking whether the result is trustworthy.
// Storage override and build-time env value are always reliable. The
// host-based fallback is only reliable on local dev (the gateway listens on
// the same host there); on a real deployment it is a guess and must be flagged.
function resolveGatewayEndpoint(
  storageKey: string,
  envValue: string | undefined,
  localFallback: string,
  hostIsLocal: boolean,
): ResolvedEndpoint {
  const fromStorage =
    typeof window !== "undefined" ? safeLocalGet(storageKey, "") : "";
  if (fromStorage.trim()) {
    return { value: fromStorage.trim(), reliable: true };
  }
  if (envValue && envValue.trim()) {
    return { value: envValue.trim(), reliable: true };
  }
  return { value: localFallback, reliable: hostIsLocal };
}

const _host = runtimeHost();
const _protocol = runtimeProtocol();
const _isLocalHost = isLocalHost(_host);
const _ports = getSystemDefaults().endpointDefaults;
const QUERY_ROUTER_URL = runtimeSetting(
  "builder.settings.queryRouterUrl",
  import.meta.env.VITE_QUERY_ROUTER_URL as string | undefined,
  `${_protocol}//${_host}:${_ports.query_router_port}`,
);
const _gatewayHttp = resolveGatewayEndpoint(
  "builder.settings.gatewayHttpUrl",
  import.meta.env.VITE_GATEWAY_URL as string | undefined,
  `${_protocol}//${_host}:${_ports.gateway_http_port}`,
  _isLocalHost,
);
const _gatewayJdbcHost = resolveGatewayEndpoint(
  "builder.settings.gatewayJdbcHost",
  import.meta.env.VITE_GATEWAY_JDBC_HOST as string | undefined,
  _host,
  _isLocalHost,
);
const GATEWAY_HTTP_URL = _gatewayHttp.value;
const GATEWAY_JDBC_HOST = _gatewayJdbcHost.value;
// True only when both gateway endpoints were resolved from an explicit source
// (settings override or build-arg) or we are on local dev. When false, the
// panel warns that the displayed gateway address may be wrong.
const GATEWAY_ENDPOINTS_RELIABLE = _gatewayHttp.reliable && _gatewayJdbcHost.reliable;
const GATEWAY_JDBC_PORT =
  runtimeSetting(
    "builder.settings.gatewayJdbcPort",
    import.meta.env.VITE_GATEWAY_JDBC_PORT as string | undefined,
    String(_ports.gateway_jdbc_port),
  );

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function CodeBlock({ code }: { code: string }) {
  const t = useT();
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }

  return (
    <Box sx={{ position: "relative" }}>
      <Box
        component="pre"
        sx={{
          m: 0,
          p: 1.5,
          pr: 5,
          bgcolor: "grey.900",
          color: "grey.100",
          borderRadius: 1,
          fontSize: "0.72rem",
          fontFamily: "monospace",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          overflowX: "auto",
        }}
      >
        {code}
      </Box>
      <Tooltip title={copied ? t("endpoints.copiedToClipboard") : t("common.copy")}>
        <IconButton
          size="small"
          onClick={handleCopy}
          sx={{
            position: "absolute",
            top: 4,
            right: 4,
            color: copied ? "success.light" : "grey.400",
            "&:hover": { color: "grey.100" },
          }}
        >
          {copied ? (
            <CheckIcon fontSize="small" />
          ) : (
            <ContentCopyIcon fontSize="small" />
          )}
        </IconButton>
      </Tooltip>
    </Box>
  );
}

// Shown above the JDBC and XMLA panels when the gateway address could not be
// reliably determined (Bug-5540). Prevents silently handing users a gateway
// URL that points at the app domain where nothing listens.
function GatewayAddressWarning() {
  const t = useT();
  if (GATEWAY_ENDPOINTS_RELIABLE) {
    return null;
  }
  return (
    <Alert severity="warning" sx={{ mb: 1.5, py: 0.5 }}>
      {t("endpoints.gatewayAddressUnverified")}
    </Alert>
  );
}

// ---------------------------------------------------------------------------
// Per-endpoint panels
// ---------------------------------------------------------------------------

interface EndpointPanelProps {
  modelId: string;
  modelSlug: string;
  tenantSlug: string;
  projectSlug: string;
}

// The `database` startup param supported by the JDBC gateway (Bug-5878):
// <tenant>, <tenant>/<model>, or <tenant>/<project>/<model>. This panel is
// opened for one specific model, so it defaults to the fully-scoped form
// when the project slug has loaded; falls back to tenant/model otherwise.
function scopedJdbcDatabase(tenantSlug: string, projectSlug: string, modelSlug: string) {
  if (projectSlug) {
    return `${tenantSlug}/${projectSlug}/${modelSlug}`;
  }
  return `${tenantSlug}/${modelSlug}`;
}

function RestApiEndpoint({ modelId, modelSlug, tenantSlug }: EndpointPanelProps) {
  const t = useT();
  const [tab, setTab] = useState(0);

  const baseUrl = `${QUERY_ROUTER_URL}/api/v1`;

  const curlExample = `curl -X POST "${baseUrl}/execute" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <your_token>" \\
  -d '{
    "model_id": "${modelId}",
    "raw_query": "SELECT region, SUM(revenue) FROM ${modelSlug} GROUP BY region",
    "protocol": "jdbc"
  }'`;

  const pythonExample = `import requests

response = requests.post(
    "${baseUrl}/execute",
    headers={
        "Authorization": "Bearer <your_token>",
        "Content-Type": "application/json",
    },
    json={
        "model_id": "${modelId}",
        "raw_query": "SELECT region, SUM(revenue) FROM ${modelSlug} GROUP BY region",
        "protocol": "jdbc",
    },
)
data = response.json()
print(data["rows"])`;

  const explainExample = `curl -X POST "${baseUrl}/explain" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <your_token>" \\
  -d '{
    "model_id": "${modelId}",
    "raw_query": "SELECT region, SUM(revenue) FROM ${modelSlug} GROUP BY region",
    "protocol": "jdbc"
  }'`;

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {t("endpoints.restApiDescription")}
      </Typography>
      <Box sx={{ display: "flex", gap: 1, mb: 1.5, flexWrap: "wrap" }}>
        <Chip label={t("endpoints.runQuery")} size="small" variant="outlined" sx={{ borderColor: ui.green, color: ui.green }} />
        <Chip label={t("endpoints.explainRoute")} size="small" color="default" variant="outlined" />
      </Box>
      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v)}
        sx={{ mb: 1, minHeight: 32 }}
        TabIndicatorProps={{ style: { height: 2 } }}
      >
        <Tab label={t("endpoints.curlTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
        <Tab label={t("endpoints.pythonTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
        <Tab label={t("endpoints.explain")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
      </Tabs>
      {tab === 0 && <CodeBlock code={curlExample} />}
      {tab === 1 && <CodeBlock code={pythonExample} />}
      {tab === 2 && <CodeBlock code={explainExample} />}
    </Box>
  );
}

function JdbcEndpoint({ modelSlug, tenantSlug, projectSlug }: EndpointPanelProps) {
  const t = useT();
  const [tab, setTab] = useState(0);

  const scopedDbName = scopedJdbcDatabase(tenantSlug, projectSlug, modelSlug);
  const jdbcUrl = `jdbc:postgresql://${GATEWAY_JDBC_HOST}:${GATEWAY_JDBC_PORT}/${scopedDbName}`;
  const psqlCmd = `psql "host=${GATEWAY_JDBC_HOST} port=${GATEWAY_JDBC_PORT} dbname=${scopedDbName} user=<your_email> password=<your_password> sslmode=prefer"

# The database name above scopes this connection to the ${modelSlug} model.
# Query using the model slug as the table name.
SELECT region, SUM(revenue) FROM ${modelSlug} GROUP BY region`;

  const pythonExample = `import psycopg2

conn = psycopg2.connect(
    host="${GATEWAY_JDBC_HOST}",
    port=${GATEWAY_JDBC_PORT},
    dbname="${scopedDbName}",
    user="<your_email>",
    password="<your_password>",
)
cur = conn.cursor()
cur.execute("SELECT region, SUM(revenue) FROM ${modelSlug} GROUP BY region")
print(cur.fetchall())`;

  const javaExample = `// Maven: org.postgresql:postgresql:42.7.3
String url = "${jdbcUrl}";
Properties props = new Properties();
props.setProperty("user", "<your_email>");
props.setProperty("password", "<your_password>");

try (Connection conn = DriverManager.getConnection(url, props);
     Statement stmt = conn.createStatement()) {
    ResultSet rs = stmt.executeQuery(
        "SELECT region, SUM(revenue) FROM ${modelSlug} GROUP BY region"
    );
    while (rs.next()) System.out.println(rs.getString(1) + " " + rs.getLong(2));
}`;

  return (
    <Box>
      <GatewayAddressWarning />
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {t("endpoints.jdbcDescription", { port: GATEWAY_JDBC_PORT })}
      </Typography>
      <Box sx={{ display: "flex", gap: 1, mb: 1.5, flexWrap: "wrap" }}>
        <Chip
          label={`${t("endpoints.host")}: ${GATEWAY_JDBC_HOST}`}
          size="small"
          variant="outlined"
        />
        <Chip
          label={`${t("endpoints.port")}: ${GATEWAY_JDBC_PORT}`}
          size="small"
          variant="outlined"
        />
        <Chip
          label={`${t("endpoints.database")}: ${scopedDbName}`}
          size="small"
          variant="outlined"
          sx={{ borderColor: ui.green, color: ui.green }}
        />
      </Box>
      <Alert severity="info" sx={{ mb: 1.5, py: 0.5 }}>
        {t("endpoints.jdbcDatabaseScopingInfo")}
      </Alert>
      <Alert severity="info" sx={{ mb: 1.5, py: 0.5 }}>
        {t("endpoints.ssoPatNote")}
      </Alert>
      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v)}
        sx={{ mb: 1, minHeight: 32 }}
        TabIndicatorProps={{ style: { height: 2 } }}
      >
        <Tab label={t("endpoints.psqlTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
        <Tab label={t("endpoints.pythonTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
        <Tab label={t("endpoints.javaTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
      </Tabs>
      {tab === 0 && <CodeBlock code={psqlCmd} />}
      {tab === 1 && <CodeBlock code={pythonExample} />}
      {tab === 2 && <CodeBlock code={javaExample} />}
      <Link
        href="/help/integrations/jdbc-connection-guide.html"
        target="_blank"
        rel="noopener"
        sx={{ fontSize: "0.8rem", display: "inline-flex", alignItems: "center", gap: 0.5, mt: 1.5 }}
      >
        <HelpOutlineIcon sx={{ fontSize: 16 }} />
        {t("endpoints.jdbcSetupGuide")}
      </Link>
    </Box>
  );
}

function XmlaEndpoint({ modelSlug, tenantSlug, projectSlug }: EndpointPanelProps) {
  const t = useT();
  // Excel handles the SSAS-style tenantless endpoint and selects the catalog
  // through XMLA properties.
  const xmlaBaseUrl = `${GATEWAY_HTTP_URL}/api/v1/xmla`;
  const xmlaServerUrl = `${xmlaBaseUrl}/`;
  const xmlaTenantUrl = `${GATEWAY_HTTP_URL}/api/v1/xmla/${tenantSlug}`;

  // Power BI Desktop uses the PostgreSQL connector (port 5433) because its
  // Analysis Services connector supports Windows authentication only, which is
  // incompatible with Tessallite's HTTP Basic XMLA endpoint.

  const curlExample = `curl -X POST "${xmlaTenantUrl}" \\
  -H "Content-Type: text/xml" \\
  -H "Authorization: Basic $(echo -n '<your_email>:<your_password>' | base64)" \\
  -d '<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">
  <Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DBSCHEMA_CATALOGS</RequestType>
      <Properties>
        <PropertyList>
          <Catalog>${tenantSlug}</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </Body>
</Envelope>'`;

  const powerBiInstructions = `1. Open Power BI Desktop
2. Get Data → Database → PostgreSQL database → Connect
3. Server: ${GATEWAY_JDBC_HOST}:${GATEWAY_JDBC_PORT}
4. Database: ${tenantSlug}
5. Data Connectivity mode: DirectQuery (recommended)
6. Click OK
7. Authentication: Database
   Username: <your_email>  (Tessallite login)
   Password: <your_password>
8. Click Connect
9. In the Navigator, select the model → Load or Transform Data`;

  const excelInstructions = `1. Data → Get Data → From Database → From Analysis Services
2. Server name: ${xmlaServerUrl}
3. Log on credentials: Use the following...
   User name: <your_email>
   Password: <your_password>
4. Select catalog/database: ${tenantSlug}
5. Select model/cube: ${modelSlug}`;

  const [tab, setTab] = useState(0);

  return (
    <Box>
      <GatewayAddressWarning />
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {t("endpoints.xmlaDescription")}
      </Typography>
      <Box sx={{ display: "flex", gap: 1, mb: 1.5, flexWrap: "wrap" }}>
        <Chip
          label={`${t("endpoints.tenantCatalog")}: ${tenantSlug || "<tenant_slug>"}`}
          size="small"
          variant="outlined"
          sx={{ borderColor: ui.green, color: ui.green }}
        />
        <Chip
          label={`${t("endpoints.model")}: ${modelSlug}`}
          size="small"
          variant="outlined"
        />
        <Chip label={t("endpoints.soapXmlaLabel")} size="small" variant="outlined" />
      </Box>
      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v)}
        sx={{ mb: 1, minHeight: 32 }}
        TabIndicatorProps={{ style: { height: 2 } }}
      >
        <Tab label={t("endpoints.powerBiTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
        <Tab label={t("endpoints.excelTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
        <Tab label={t("endpoints.soapCurlTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
      </Tabs>
      {tab === 0 && (
        <Box>
          <Alert severity="info" sx={{ mb: 1.5, py: 0.5 }}>
            {t("endpoints.powerBiAuthNote")}
          </Alert>
          <Alert severity="info" sx={{ mb: 1.5, py: 0.5 }}>
            {t("endpoints.ssoPatNote")}
          </Alert>
          <Box sx={{ display: "flex", gap: 1, mb: 1.5, flexWrap: "wrap" }}>
            <Chip
              label={`${t("endpoints.host")}: ${GATEWAY_JDBC_HOST}`}
              size="small"
              variant="outlined"
            />
            <Chip
              label={`${t("endpoints.port")}: ${GATEWAY_JDBC_PORT}`}
              size="small"
              variant="outlined"
            />
            <Chip
              label={`${t("endpoints.database")}: ${tenantSlug}`}
              size="small"
              variant="outlined"
              sx={{ borderColor: ui.green, color: ui.green }}
            />
          </Box>
          <CodeBlock code={powerBiInstructions} />
          <Link
            href="/help/integrations/powerbi-connection-guide.html"
            target="_blank"
            rel="noopener"
            sx={{ fontSize: "0.8rem", display: "inline-flex", alignItems: "center", gap: 0.5, mt: 1.5 }}
          >
            <HelpOutlineIcon sx={{ fontSize: 16 }} />
            {t("endpoints.powerBiTab")}
          </Link>
        </Box>
      )}
      {tab === 1 && (
        <Box>
          <Alert severity="info" sx={{ mb: 1.5, py: 0.5 }}>
            {t("endpoints.ssoPatNote")}
          </Alert>
          <CodeBlock code={excelInstructions} />
        </Box>
      )}
      {tab === 2 && <CodeBlock code={curlExample} />}
    </Box>
  );
}

export function generateManifest(baseUrl: string): string {
  const u = baseUrl.replace(/\/$/, "");
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<OfficeApp
  xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
  xmlns:ov="http://schemas.microsoft.com/office/taskpaneappversionoverrides"
  xsi:type="TaskPaneApp">

  <Id>e3ae536b-1d2f-44fd-bdc4-e01b5a7597d2</Id>
  <Version>1.0.0.9</Version>
  <ProviderName>Tessallite</ProviderName>
  <DefaultLocale>en-US</DefaultLocale>
  <DisplayName DefaultValue="Tessallite"/>
  <Description DefaultValue="Governed semantic layer insights inside Excel."/>

  <IconUrl DefaultValue="${u}/excel-plugin/assets/icon-32.png"/>
  <HighResolutionIconUrl DefaultValue="${u}/excel-plugin/assets/icon-80.png"/>

  <SupportUrl DefaultValue="${u}/help/excel-plugin.html"/>

  <AppDomains>
    <AppDomain>${u}</AppDomain>
  </AppDomains>

  <Hosts>
    <Host Name="Workbook"/>
  </Hosts>

  <Requirements>
    <Sets DefaultMinVersion="1.1">
      <Set Name="CustomFunctionsRuntime" MinVersion="1.1"/>
    </Sets>
  </Requirements>

  <DefaultSettings>
    <SourceLocation DefaultValue="${u}/excel-plugin/index.html"/>
  </DefaultSettings>

  <Permissions>ReadWriteDocument</Permissions>

  <VersionOverrides xmlns="http://schemas.microsoft.com/office/taskpaneappversionoverrides" xsi:type="VersionOverridesV1_0">
    <Hosts>
      <Host xsi:type="Workbook">
        <AllFormFactors>
          <ExtensionPoint xsi:type="CustomFunctions">
            <Script>
              <SourceLocation resid="Functions.Script.Url"/>
            </Script>
            <Page>
              <SourceLocation resid="Functions.Page.Url"/>
            </Page>
            <Metadata>
              <SourceLocation resid="Functions.Metadata.Url"/>
            </Metadata>
            <Namespace resid="Functions.Namespace"/>
          </ExtensionPoint>
        </AllFormFactors>
        <DesktopFormFactor>
          <FunctionFile resid="Functions.Page.Url"/>
          <ExtensionPoint xsi:type="PrimaryCommandSurface">
            <OfficeTab id="TabHome">
              <Group id="Tessallite.Group">
                <Label resid="GroupLabel"/>
                <Icon>
                  <bt:Image size="16" resid="Icon16"/>
                  <bt:Image size="32" resid="Icon32"/>
                  <bt:Image size="80" resid="Icon80"/>
                </Icon>
                <Control xsi:type="Button" id="Tessallite.OpenPane">
                  <Label resid="OpenPane.Label"/>
                  <Supertip>
                    <Title resid="OpenPane.Label"/>
                    <Description resid="OpenPane.Tooltip"/>
                  </Supertip>
                  <Icon>
                    <bt:Image size="16" resid="Icon16"/>
                    <bt:Image size="32" resid="Icon32"/>
                    <bt:Image size="80" resid="Icon80"/>
                  </Icon>
                  <Action xsi:type="ShowTaskpane">
                    <TaskpaneId>TessallitePane</TaskpaneId>
                    <SourceLocation resid="Taskpane.Url"/>
                  </Action>
                </Control>
              </Group>
            </OfficeTab>
          </ExtensionPoint>
        </DesktopFormFactor>
      </Host>
    </Hosts>

    <Resources>
      <bt:Images>
        <bt:Image id="Icon16" DefaultValue="${u}/excel-plugin/assets/icon-16.png"/>
        <bt:Image id="Icon32" DefaultValue="${u}/excel-plugin/assets/icon-32.png"/>
        <bt:Image id="Icon80" DefaultValue="${u}/excel-plugin/assets/icon-80.png"/>
      </bt:Images>
      <bt:Urls>
        <bt:Url id="Taskpane.Url" DefaultValue="${u}/excel-plugin/index.html"/>
        <bt:Url id="Functions.Script.Url" DefaultValue="${u}/excel-plugin/functions.iife.js"/>
        <bt:Url id="Functions.Page.Url" DefaultValue="${u}/excel-plugin/functions.html"/>
        <bt:Url id="Functions.Metadata.Url" DefaultValue="${u}/excel-plugin/functions.json"/>
      </bt:Urls>
      <bt:ShortStrings>
        <bt:String id="GroupLabel" DefaultValue="Tessallite"/>
        <bt:String id="OpenPane.Label" DefaultValue="Tessallite"/>
        <bt:String id="Functions.Namespace" DefaultValue="TESSALLITE"/>
      </bt:ShortStrings>
      <bt:LongStrings>
        <bt:String id="OpenPane.Tooltip" DefaultValue="Open Tessallite panel."/>
      </bt:LongStrings>
    </Resources>
  </VersionOverrides>
</OfficeApp>`;
}

function McpEndpoint({ tenantSlug }: EndpointPanelProps) {
  const t = useT();

  // In GCP the model service is nginx-proxied at the app domain (VITE_MODEL_SERVICE_URL
  // = https://cloud.tessallite.io). Locally the var is unset and we fall back to the
  // direct port (http://localhost:8001).
  const modelServiceUrl =
    (import.meta.env.VITE_MODEL_SERVICE_URL as string | undefined)?.trim() ||
    `${_protocol}//${_host}:${_ports.model_service_port}`;
  const queryRouterUrl = QUERY_ROUTER_URL.replace(/\/api\/v1$/, "");

  const claudeConfig = `{
  "mcpServers": {
    "tessallite": {
      "command": "tessallite-mcp",
      "env": {
        "TESSALLITE_URL": "${modelServiceUrl}",
        "TESSALLITE_QUERY_URL": "${queryRouterUrl}",
        "TESSALLITE_TENANT_ID": "${tenantSlug}",
        "TESSALLITE_EMAIL": "<your_email>",
        "TESSALLITE_PASSWORD": "<your_password>"
      }
    }
  }
}`;

  const pipInstall = `pip install tessallite-mcp
# or from the monorepo:
cd tessallite/mcp-server && pip install -e .`;

  const [tab, setTab] = useState(0);

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {t("endpoints.mcpDescription")}
      </Typography>
      <Box sx={{ display: "flex", gap: 1, mb: 1.5, flexWrap: "wrap" }}>
        <Chip label={`${t("endpoints.tenant")}: ${tenantSlug}`} size="small" variant="outlined" sx={{ borderColor: ui.green, color: ui.green }} />
        <Chip label={t("endpoints.stdioTransport")} size="small" variant="outlined" />
      </Box>
      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v)}
        sx={{ mb: 1, minHeight: 32 }}
        TabIndicatorProps={{ style: { height: 2 } }}
      >
        <Tab label={t("endpoints.claudeDesktopTab")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
        <Tab label={t("endpoints.install")} sx={{ minHeight: 32, py: 0, textTransform: "none" }} />
      </Tabs>
      {tab === 0 && <CodeBlock code={claudeConfig} />}
      {tab === 1 && <CodeBlock code={pipInstall} />}
      <Alert severity="info" sx={{ mt: 1.5, py: 0.5 }}>
        <Typography variant="body2">
          {t("endpoints.availableTools")}
        </Typography>
      </Alert>
    </Box>
  );
}

function ExcelPluginEndpoint({ tenantSlug }: EndpointPanelProps) {
  const t = useT();
  const [serverUrl, setServerUrl] = useState(() => {
    const origin = window.location.origin;
    if (origin === 'http://localhost:3000') return 'https://localhost:3443';
    return origin;
  });

  function handleDownload() {
    const xml = generateManifest(serverUrl);
    const blob = new Blob([xml], { type: "application/xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "manifest.xml";
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Box>
      <Alert severity="success" sx={{ mb: 1.5 }}>
        {t("endpoints.excelPluginAlert")}
      </Alert>

      {/* Primary: dynamically generated manifest using the current server URL */}
      <Box sx={{ mb: 1.5 }}>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 0.75 }}>
          {t("endpoints.deployedManifestInfo")}
        </Typography>
        <Button
          variant="contained"
          size="small"
          startIcon={<DownloadIcon />}
          onClick={handleDownload}
          sx={{ textTransform: "none", mr: 1.5 }}
        >
          {t("endpoints.downloadDeployedManifest")}
        </Button>
        <Link
          href="/help/excel-plugin.html"
          target="_blank"
          rel="noopener"
          sx={{ fontSize: "0.8rem", display: "inline-flex", alignItems: "center", gap: 0.5 }}
        >
          <HelpOutlineIcon sx={{ fontSize: 16 }} />
          {t("endpoints.setupGuide")}
        </Link>
      </Box>

      {/* Secondary: generate a manifest for a custom server URL */}
      <Typography variant="body2" color="text.secondary" sx={{ mb: 0.75 }}>
        {t("endpoints.customManifestInfo")}
      </Typography>
      <TextField
        label={t("endpoints.serverUrlLabel")}
        size="small"
        fullWidth
        value={serverUrl}
        onChange={(e) => setServerUrl(e.target.value)}
        helperText={t("endpoints.serverUrlHelperText")}
        InputLabelProps={{ shrink: true }}
        sx={{ mb: 1 }}
      />
      <Box sx={{ display: "flex", gap: 1, mb: 1.5, flexWrap: "wrap" }}>
        <Chip
          label={`${t("endpoints.tenant")}: ${tenantSlug}`}
          size="small"
          variant="outlined"
          sx={{ borderColor: ui.green, color: ui.green }}
        />
      </Box>
      <Button
        variant="outlined"
        size="small"
        startIcon={<DownloadIcon />}
        onClick={handleDownload}
        sx={{ textTransform: "none", mr: 1.5 }}
      >
        {t("endpoints.downloadManifest")}
      </Button>

      <Alert severity="info" sx={{ mt: 2, py: 0.5 }}>
        <Typography variant="body2" fontWeight={600} sx={{ mb: 0.25 }}>
          {t("endpoints.deploymentOptions")}
        </Typography>
        <Typography variant="body2" component="div">
          {t("endpoints.deploymentOptionsText")}
        </Typography>
      </Alert>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export default function EndpointsPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const model = useModel(projectId!, modelId!);
  const project = useProject(projectId!);
  const tenantSlug = safeLocalGet("tenant_id", "");
  const modelSlug = model.data?.slug ?? modelId ?? "";
  const projectSlug = project.data?.slug ?? "";

  const props: EndpointPanelProps = {
    modelId: modelId!,
    modelSlug,
    tenantSlug,
    projectSlug,
  };

  const [expanded, setExpanded] = useState<string | false>("rest");

  function toggle(panel: string) {
    setExpanded((prev) => (prev === panel ? false : panel));
  }

  return (
    <Box sx={{ p: 2 }}>
      <Typography variant="subtitle2" fontWeight={700} sx={{ mb: 0.5 }}>
        {t("panels.endpoints")}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        {t("endpoints.description")}
      </Typography>

      <Alert severity="info" sx={{ mb: 2, py: 0.5 }}>
        {t("endpoints.authenticationInfo")}
      </Alert>

      <Alert severity="success" sx={{ mb: 2, py: 0.5 }}>
        <Typography variant="body2" fontWeight={600} sx={{ mb: 0.25 }}>
          {t("endpoints.modelExposedAsTwo")}
        </Typography>
        <Typography variant="body2" component="div">
          {t("endpoints.modelViewsInfo", { modelSlug })}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          {t("endpoints.sameConnectionString")}
        </Typography>
      </Alert>

      {/* REST / Query Router */}
      <Accordion
        expanded={expanded === "rest"}
        onChange={() => toggle("rest")}
        disableGutters
        elevation={0}
        sx={{ border: 1, borderColor: "divider", mb: 1, "&:before": { display: "none" } }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 40 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <Typography variant="body2" fontWeight={600}>
              {t("endpoints.restApiTitle")}
            </Typography>
            <Chip label={t("endpoints.httpJsonLabel")} size="small" sx={{ bgcolor: ui.greenBg, color: ui.green, fontWeight: 500 }} />
          </Box>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <RestApiEndpoint {...props} />
        </AccordionDetails>
      </Accordion>

      {/* JDBC */}
      <Accordion
        expanded={expanded === "jdbc"}
        onChange={() => toggle("jdbc")}
        disableGutters
        elevation={0}
        sx={{ border: 1, borderColor: "divider", mb: 1, "&:before": { display: "none" } }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 40 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <Typography variant="body2" fontWeight={600}>
              {t("endpoints.jdbcTitle")}
            </Typography>
            <Chip label={`${t("endpoints.port")} ${GATEWAY_JDBC_PORT}`} size="small" sx={{ bgcolor: ui.purpleBg, color: ui.purple, fontWeight: 500 }} />
          </Box>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <JdbcEndpoint {...props} />
        </AccordionDetails>
      </Accordion>

      {/* XMLA / DAX */}
      <Accordion
        expanded={expanded === "xmla"}
        onChange={() => toggle("xmla")}
        disableGutters
        elevation={0}
        sx={{ border: 1, borderColor: "divider", mb: 1, "&:before": { display: "none" } }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 40 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <Typography variant="body2" fontWeight={600}>
              {t("endpoints.xmlaTitle")}
            </Typography>
            <Chip label={t("endpoints.powerBiExcelLabel")} size="small" color="success" />
          </Box>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <XmlaEndpoint {...props} />
        </AccordionDetails>
      </Accordion>

      {/* Excel Plugin */}
      <Accordion
        expanded={expanded === "excel"}
        onChange={() => toggle("excel")}
        disableGutters
        elevation={0}
        sx={{ border: 1, borderColor: "divider", "&:before": { display: "none" } }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 40 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <Typography variant="body2" fontWeight={600}>
              {t("endpoints.excelPluginTitle")}
            </Typography>
            <Chip label={t("endpoints.taskPaneAddinLabel")} size="small" sx={{ bgcolor: ui.greenBg, color: ui.green, fontWeight: 500 }} />
          </Box>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <ExcelPluginEndpoint {...props} />
        </AccordionDetails>
      </Accordion>

      {/* MCP Server */}
      <Accordion
        expanded={expanded === "mcp"}
        onChange={() => toggle("mcp")}
        disableGutters
        elevation={0}
        sx={{ border: 1, borderColor: "divider", mb: 1, "&:before": { display: "none" } }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 40 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <Typography variant="body2" fontWeight={600}>
              {t("endpoints.mcpServerTitle")}
            </Typography>
            <Chip label={t("endpoints.aiAssistantsLabel")} size="small" sx={{ bgcolor: ui.purpleBg, color: ui.purple, fontWeight: 500 }} />
          </Box>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <McpEndpoint {...props} />
        </AccordionDetails>
      </Accordion>

      {/* API Documentation */}
      <Box sx={{ mt: 2, display: "flex", gap: 2, flexWrap: "wrap" }}>
        <Link
          href="/api/v1/docs"
          target="_blank"
          rel="noopener"
          sx={{ fontSize: "0.8rem", display: "inline-flex", alignItems: "center", gap: 0.5 }}
        >
          <HelpOutlineIcon sx={{ fontSize: 16 }} />
          {t("endpoints.modelServiceApiDocs")}
        </Link>
        <Link
          href={`${QUERY_ROUTER_URL.replace(/\/api\/v1$/, "")}/docs`}
          target="_blank"
          rel="noopener"
          sx={{ fontSize: "0.8rem", display: "inline-flex", alignItems: "center", gap: 0.5 }}
        >
          <HelpOutlineIcon sx={{ fontSize: 16 }} />
          {t("endpoints.queryRouterApiDocs")}
        </Link>
      </Box>
    </Box>
  );
}
