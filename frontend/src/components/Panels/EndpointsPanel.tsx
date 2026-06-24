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
import { useModel } from "../../api/hooks";
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

const _host = runtimeHost();
const _protocol = runtimeProtocol();
const _ports = getSystemDefaults().endpointDefaults;
const QUERY_ROUTER_URL = runtimeSetting(
  "builder.settings.queryRouterUrl",
  import.meta.env.VITE_QUERY_ROUTER_URL as string | undefined,
  `${_protocol}//${_host}:${_ports.query_router_port}`,
);
const GATEWAY_HTTP_URL = runtimeSetting(
  "builder.settings.gatewayHttpUrl",
  import.meta.env.VITE_GATEWAY_URL as string | undefined,
  `${_protocol}//${_host}:${_ports.gateway_http_port}`,
);
const GATEWAY_JDBC_HOST = runtimeSetting(
  "builder.settings.gatewayJdbcHost",
  import.meta.env.VITE_GATEWAY_JDBC_HOST as string | undefined,
  _host,
);
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

// ---------------------------------------------------------------------------
// Per-endpoint panels
// ---------------------------------------------------------------------------

interface EndpointPanelProps {
  modelId: string;
  modelSlug: string;
  tenantSlug: string;
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

function JdbcEndpoint({ modelId, modelSlug, tenantSlug }: EndpointPanelProps) {
  const t = useT();
  const [tab, setTab] = useState(0);

  const jdbcUrl = `jdbc:postgresql://${GATEWAY_JDBC_HOST}:${GATEWAY_JDBC_PORT}/${tenantSlug}?model_id=${modelId}`;
  const psqlCmd = `psql "host=${GATEWAY_JDBC_HOST} port=${GATEWAY_JDBC_PORT} dbname=${tenantSlug} user=<your_email> password=<your_password> sslmode=prefer"

# Query using the model slug as the table name.
# The gateway resolves the model automatically.
SELECT region, SUM(revenue) FROM ${modelSlug} GROUP BY region`;

  const pythonExample = `import psycopg2

conn = psycopg2.connect(
    host="${GATEWAY_JDBC_HOST}",
    port=${GATEWAY_JDBC_PORT},
    dbname="${tenantSlug}",
    user="<your_email>",
    password="<your_password>",
    options="-c model_id=${modelId}",
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
          label={`${t("endpoints.database")}: ${tenantSlug}`}
          size="small"
          variant="outlined"
          sx={{ borderColor: ui.green, color: ui.green }}
        />
      </Box>
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
    </Box>
  );
}

function XmlaEndpoint({ modelSlug, tenantSlug }: EndpointPanelProps) {
  const t = useT();
  // Generic endpoint — Excel sends Catalog in the SOAP envelope
  const xmlaBaseUrl = `${GATEWAY_HTTP_URL}/api/v1/xmla`;
  // Tenant-specific endpoint — Power BI and direct API callers
  const xmlaTenantUrl = `${GATEWAY_HTTP_URL}/api/v1/xmla/${tenantSlug}`;

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
2. Get Data → Analysis Services
3. Server: ${xmlaTenantUrl}
4. Authentication: Basic
   Username: <your_email>
   Password: <your_password>
5. Select cube/model: ${modelSlug}`;

  const excelInstructions = `1. Data → Get Data → From Database → From Analysis Services
2. Server name: ${xmlaBaseUrl}
3. Log on credentials: Use the following...
   User name: <your_email>
   Password: <your_password>
4. Select catalog/database: ${tenantSlug}
5. Select model/cube: ${modelSlug}`;

  const connectionString = `Provider=MSOLAP;Data Source=${xmlaTenantUrl};Catalog=${tenantSlug};`;

  const [tab, setTab] = useState(0);
  const [connCopied, setConnCopied] = useState(false);

  function handleCopyConnString() {
    navigator.clipboard.writeText(connectionString).then(() => {
      setConnCopied(true);
      setTimeout(() => setConnCopied(false), 1800);
    });
  }

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
        {t("endpoints.xmlaDescription")}
      </Typography>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1.5 }}>
        <Typography variant="caption" color="text.secondary">
          {t("endpoints.connectionString")}
        </Typography>
        <Box
          component="code"
          sx={{
            flex: 1,
            fontSize: "0.7rem",
            fontFamily: "monospace",
            bgcolor: "grey.900",
            color: "grey.100",
            px: 1,
            py: 0.5,
            borderRadius: 0.5,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {connectionString}
        </Box>
        <Tooltip title={connCopied ? t("endpoints.copiedToClipboard") : t("endpoints.copyConnectionString")}>
          <IconButton size="small" onClick={handleCopyConnString}>
            {connCopied ? (
              <CheckIcon fontSize="small" color="success" />
            ) : (
              <ContentCopyIcon fontSize="small" />
            )}
          </IconButton>
        </Tooltip>
      </Box>
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
      {tab === 0 && <CodeBlock code={powerBiInstructions} />}
      {tab === 1 && <CodeBlock code={excelInstructions} />}
      {tab === 2 && <CodeBlock code={curlExample} />}
    </Box>
  );
}

function generateManifest(baseUrl: string): string {
  const u = baseUrl.replace(/\/$/, "");
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<OfficeApp
  xmlns="http://schemas.microsoft.com/office/appforoffice/1.1"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:bt="http://schemas.microsoft.com/office/officeappbasictypes/1.0"
  xmlns:ov="http://schemas.microsoft.com/office/taskpaneappversionoverrides"
  xsi:type="TaskPaneApp">

  <Id>e3ae536b-1d2f-44fd-bdc4-e01b5a7597d2</Id>
  <Version>1.0.0.0</Version>
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

  <DefaultSettings>
    <SourceLocation DefaultValue="${u}/excel-plugin/index.html"/>
  </DefaultSettings>

  <Permissions>ReadWriteDocument</Permissions>

  <VersionOverrides xmlns="http://schemas.microsoft.com/office/taskpaneappversionoverrides" xsi:type="VersionOverridesV1_0">
    <Hosts>
      <Host xsi:type="Workbook">
        <DesktopFormFactor>
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
      </bt:Urls>
      <bt:ShortStrings>
        <bt:String id="GroupLabel" DefaultValue="Tessallite"/>
        <bt:String id="OpenPane.Label" DefaultValue="Tessallite"/>
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
  // Default to the current origin — on cloud this is https://cloud.tessallite.io.
  // On local dev it is https://localhost:3443 (or http://localhost:3000).
  const [serverUrl, setServerUrl] = useState(() => window.location.origin);

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

      {/* Primary: download the server-generated, always-current manifest */}
      <Box sx={{ mb: 1.5 }}>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 0.75 }}>
          {t("endpoints.deployedManifestInfo")}
        </Typography>
        <Button
          variant="contained"
          size="small"
          startIcon={<DownloadIcon />}
          component="a"
          href="/excel-plugin/manifest.xml"
          download="manifest.xml"
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
  const tenantSlug = safeLocalGet("tenant_id", "");
  const modelSlug = model.data?.slug ?? modelId ?? "";

  const props: EndpointPanelProps = {
    modelId: modelId!,
    modelSlug,
    tenantSlug,
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
