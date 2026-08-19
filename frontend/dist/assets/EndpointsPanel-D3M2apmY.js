import{r as T,j as e}from"./react-vendor-DnqIm-AP.js";import{u as v,a5 as J,a4 as Q,s as z,aA as a,aM as X}from"./index-CfYnQI4X.js";import{a6 as w,bb as E,aE as k,n as K,o as Z}from"./mui-icons-BQU9WjcW.js";import{b as ee}from"./react-router-BJbdiX_o.js";import{B as i,a as l,j as b,ad as I,ae as _,C as c,af as D,v as L,N as U,O as g,q as B,o as ne,T as se,I as te}from"./mui-core-_A6hzG3g.js";import"./vendor-BFYrPwbP.js";import"./react-query-D-geTpFl.js";function oe(){return typeof window>"u"?"localhost":window.location.hostname||"localhost"}function ie(){return typeof window>"u"?"http:":window.location.protocol||"http:"}function M(n,s,o){const t=typeof window<"u"?z(n,""):"";return t.trim()?t.trim():s??o}function re(n){return n==="localhost"||n==="127.0.0.1"||n==="0.0.0.0"||n==="::1"||n==="[::1]"||n.endsWith(".local")||n.endsWith(".localhost")}function W(n,s,o,t){const r=typeof window<"u"?z(n,""):"";return r.trim()?{value:r.trim(),reliable:!0}:{value:o,reliable:t}}const P=oe(),H=ie(),G=re(P),A=X().endpointDefaults,R=M("builder.settings.queryRouterUrl",void 0,`${H}//${P}:${A.query_router_port}`),V=W("builder.settings.gatewayHttpUrl",void 0,`${H}//${P}:${A.gateway_http_port}`,G),q=W("builder.settings.gatewayJdbcHost",void 0,P,G),F=V.value,S=q.value,ae=V.reliable&&q.reliable,j=M("builder.settings.gatewayJdbcPort",void 0,String(A.gateway_jdbc_port));function h({code:n}){const s=v(),[o,t]=T.useState(!1);function r(){navigator.clipboard.writeText(n).then(()=>{t(!0),setTimeout(()=>t(!1),1800)})}return e.jsxs(i,{sx:{position:"relative"},children:[e.jsx(i,{component:"pre",sx:{m:0,p:1.5,pr:5,bgcolor:"grey.900",color:"grey.100",borderRadius:1,fontSize:"0.72rem",fontFamily:"monospace",whiteSpace:"pre-wrap",wordBreak:"break-all",overflowX:"auto"},children:n}),e.jsx(se,{title:s(o?"endpoints.copiedToClipboard":"common.copy"),children:e.jsx(te,{size:"small",onClick:r,sx:{position:"absolute",top:4,right:4,color:o?"success.light":"grey.400","&:hover":{color:"grey.100"}},children:o?e.jsx(K,{fontSize:"small"}):e.jsx(Z,{fontSize:"small"})})})]})}function N(){const n=v();return ae?null:e.jsx(b,{severity:"warning",sx:{mb:1.5,py:.5},children:n("endpoints.gatewayAddressUnverified")})}function le(n,s,o){return s?`${n}/${s}/${o}`:`${n}/${o}`}function ce({modelId:n,modelSlug:s,tenantSlug:o}){const t=v(),[r,p]=T.useState(0),d=`${R}/api/v1`,m=`curl -X POST "${d}/execute" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <your_token>" \\
  -d '{
    "model_id": "${n}",
    "raw_query": "SELECT region, SUM(revenue) FROM ${s} GROUP BY region",
    "protocol": "jdbc"
  }'`,x=`import requests

response = requests.post(
    "${d}/execute",
    headers={
        "Authorization": "Bearer <your_token>",
        "Content-Type": "application/json",
    },
    json={
        "model_id": "${n}",
        "raw_query": "SELECT region, SUM(revenue) FROM ${s} GROUP BY region",
        "protocol": "jdbc",
    },
)
data = response.json()
print(data["rows"])`,u=`curl -X POST "${d}/explain" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <your_token>" \\
  -d '{
    "model_id": "${n}",
    "raw_query": "SELECT region, SUM(revenue) FROM ${s} GROUP BY region",
    "protocol": "jdbc"
  }'`;return e.jsxs(i,{children:[e.jsx(l,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:t("endpoints.restApiDescription")}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(c,{label:t("endpoints.runQuery"),size:"small",variant:"outlined",sx:{borderColor:a.green,color:a.green}}),e.jsx(c,{label:t("endpoints.explainRoute"),size:"small",color:"default",variant:"outlined"})]}),e.jsxs(U,{value:r,onChange:(y,f)=>p(f),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(g,{label:t("endpoints.curlTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(g,{label:t("endpoints.pythonTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(g,{label:t("endpoints.explain"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),r===0&&e.jsx(h,{code:m}),r===1&&e.jsx(h,{code:x}),r===2&&e.jsx(h,{code:u})]})}function de({modelSlug:n,tenantSlug:s,projectSlug:o}){const t=v(),[r,p]=T.useState(0),d=le(s,o,n),m=`jdbc:postgresql://${S}:${j}/${d}`,x=`psql "host=${S} port=${j} dbname=${d} user=<your_email> password=<your_password> sslmode=prefer"

# The database name above scopes this connection to the ${n} model.
# Query using the model slug as the table name.
SELECT region, SUM(revenue) FROM ${n} GROUP BY region`,u=`import psycopg2

conn = psycopg2.connect(
    host="${S}",
    port=${j},
    dbname="${d}",
    user="<your_email>",
    password="<your_password>",
)
cur = conn.cursor()
cur.execute("SELECT region, SUM(revenue) FROM ${n} GROUP BY region")
print(cur.fetchall())`,y=`// Maven: org.postgresql:postgresql:42.7.3
String url = "${m}";
Properties props = new Properties();
props.setProperty("user", "<your_email>");
props.setProperty("password", "<your_password>");

try (Connection conn = DriverManager.getConnection(url, props);
     Statement stmt = conn.createStatement()) {
    ResultSet rs = stmt.executeQuery(
        "SELECT region, SUM(revenue) FROM ${n} GROUP BY region"
    );
    while (rs.next()) System.out.println(rs.getString(1) + " " + rs.getLong(2));
}`;return e.jsxs(i,{children:[e.jsx(N,{}),e.jsx(l,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:t("endpoints.jdbcDescription",{port:j})}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(c,{label:`${t("endpoints.host")}: ${S}`,size:"small",variant:"outlined"}),e.jsx(c,{label:`${t("endpoints.port")}: ${j}`,size:"small",variant:"outlined"}),e.jsx(c,{label:`${t("endpoints.database")}: ${d}`,size:"small",variant:"outlined",sx:{borderColor:a.green,color:a.green}})]}),e.jsx(b,{severity:"info",sx:{mb:1.5,py:.5},children:t("endpoints.jdbcDatabaseScopingInfo")}),e.jsx(b,{severity:"info",sx:{mb:1.5,py:.5},children:t("endpoints.ssoPatNote")}),e.jsxs(U,{value:r,onChange:(f,$)=>p($),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(g,{label:t("endpoints.psqlTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(g,{label:t("endpoints.pythonTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(g,{label:t("endpoints.javaTab"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),r===0&&e.jsx(h,{code:x}),r===1&&e.jsx(h,{code:u}),r===2&&e.jsx(h,{code:y}),e.jsxs(L,{href:"/help/integrations/jdbc-connection-guide.html",target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5,mt:1.5},children:[e.jsx(E,{sx:{fontSize:16}}),t("endpoints.jdbcSetupGuide")]})]})}function pe({modelSlug:n,tenantSlug:s,projectSlug:o}){const t=v(),p=`${`${F}/api/v1/xmla`}/`,m=`curl -X POST "${`${F}/api/v1/xmla/${s}`}" \\
  -H "Content-Type: text/xml" \\
  -H "Authorization: Basic $(echo -n '<your_email>:<your_password>' | base64)" \\
  -d '<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">
  <Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DBSCHEMA_CATALOGS</RequestType>
      <Properties>
        <PropertyList>
          <Catalog>${s}</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </Body>
</Envelope>'`,x=`1. Open Power BI Desktop
2. Get Data → Database → PostgreSQL database → Connect
3. Server: ${S}:${j}
4. Database: ${s}
5. Data Connectivity mode: DirectQuery (recommended)
6. Click OK
7. Authentication: Database
   Username: <your_email>  (Tessallite login)
   Password: <your_password>
8. Click Connect
9. In the Navigator, select the model → Load or Transform Data`,u=`1. Data → Get Data → From Database → From Analysis Services
2. Server name: ${p}
3. Log on credentials: Use the following...
   User name: <your_email>
   Password: <your_password>
4. Select catalog/database: ${s}
5. Select model/cube: ${n}`,[y,f]=T.useState(0);return e.jsxs(i,{children:[e.jsx(N,{}),e.jsx(l,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:t("endpoints.xmlaDescription")}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(c,{label:`${t("endpoints.tenantCatalog")}: ${s||"<tenant_slug>"}`,size:"small",variant:"outlined",sx:{borderColor:a.green,color:a.green}}),e.jsx(c,{label:`${t("endpoints.model")}: ${n}`,size:"small",variant:"outlined"}),e.jsx(c,{label:t("endpoints.soapXmlaLabel"),size:"small",variant:"outlined"})]}),e.jsxs(U,{value:y,onChange:($,C)=>f(C),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(g,{label:t("endpoints.powerBiTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(g,{label:t("endpoints.excelTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(g,{label:t("endpoints.soapCurlTab"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),y===0&&e.jsxs(i,{children:[e.jsx(b,{severity:"info",sx:{mb:1.5,py:.5},children:t("endpoints.powerBiAuthNote")}),e.jsx(b,{severity:"info",sx:{mb:1.5,py:.5},children:t("endpoints.ssoPatNote")}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(c,{label:`${t("endpoints.host")}: ${S}`,size:"small",variant:"outlined"}),e.jsx(c,{label:`${t("endpoints.port")}: ${j}`,size:"small",variant:"outlined"}),e.jsx(c,{label:`${t("endpoints.database")}: ${s}`,size:"small",variant:"outlined",sx:{borderColor:a.green,color:a.green}})]}),e.jsx(h,{code:x}),e.jsxs(L,{href:"/help/integrations/powerbi-connection-guide.html",target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5,mt:1.5},children:[e.jsx(E,{sx:{fontSize:16}}),t("endpoints.powerBiTab")]})]}),y===1&&e.jsxs(i,{children:[e.jsx(b,{severity:"info",sx:{mb:1.5,py:.5},children:t("endpoints.ssoPatNote")}),e.jsx(h,{code:u})]}),y===2&&e.jsx(h,{code:m})]})}function xe(n){const s=n.replace(/\/$/,"");return`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
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

  <IconUrl DefaultValue="${s}/excel-plugin/assets/icon-32.png"/>
  <HighResolutionIconUrl DefaultValue="${s}/excel-plugin/assets/icon-80.png"/>

  <SupportUrl DefaultValue="${s}/help/excel-plugin.html"/>

  <AppDomains>
    <AppDomain>${s}</AppDomain>
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
    <SourceLocation DefaultValue="${s}/excel-plugin/index.html"/>
  </DefaultSettings>

  <Permissions>ReadWriteDocument</Permissions>

  <VersionOverrides xmlns="http://schemas.microsoft.com/office/taskpaneappversionoverrides" xsi:type="VersionOverridesV1_0">
    <Hosts>
      <Host xsi:type="Workbook">
        <AllFormFactors>
          <ExtensionPoint xsi:type="CustomFunctions">
            <Script>
              <SourceLocation resid="Functions.Script.Url"/>
            <\/Script>
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
        <bt:Image id="Icon16" DefaultValue="${s}/excel-plugin/assets/icon-16.png"/>
        <bt:Image id="Icon32" DefaultValue="${s}/excel-plugin/assets/icon-32.png"/>
        <bt:Image id="Icon80" DefaultValue="${s}/excel-plugin/assets/icon-80.png"/>
      </bt:Images>
      <bt:Urls>
        <bt:Url id="Taskpane.Url" DefaultValue="${s}/excel-plugin/index.html"/>
        <bt:Url id="Functions.Script.Url" DefaultValue="${s}/excel-plugin/functions.iife.js"/>
        <bt:Url id="Functions.Page.Url" DefaultValue="${s}/excel-plugin/functions.html"/>
        <bt:Url id="Functions.Metadata.Url" DefaultValue="${s}/excel-plugin/functions.json"/>
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
</OfficeApp>`}function ue({tenantSlug:n}){const s=v(),o=`${H}//${P}:${A.model_service_port}`,t=R.replace(/\/api\/v1$/,""),r=`{
  "mcpServers": {
    "tessallite": {
      "command": "tessallite-mcp",
      "env": {
        "TESSALLITE_URL": "${o}",
        "TESSALLITE_QUERY_URL": "${t}",
        "TESSALLITE_TENANT_ID": "${n}",
        "TESSALLITE_EMAIL": "<your_email>",
        "TESSALLITE_PASSWORD": "<your_password>"
      }
    }
  }
}`,p=`pip install tessallite-mcp
# or from the monorepo:
cd tessallite/mcp-server && pip install -e .`,[d,m]=T.useState(0);return e.jsxs(i,{children:[e.jsx(l,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:s("endpoints.mcpDescription")}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(c,{label:`${s("endpoints.tenant")}: ${n}`,size:"small",variant:"outlined",sx:{borderColor:a.green,color:a.green}}),e.jsx(c,{label:s("endpoints.stdioTransport"),size:"small",variant:"outlined"})]}),e.jsxs(U,{value:d,onChange:(x,u)=>m(u),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(g,{label:s("endpoints.claudeDesktopTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(g,{label:s("endpoints.install"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),d===0&&e.jsx(h,{code:r}),d===1&&e.jsx(h,{code:p}),e.jsx(b,{severity:"info",sx:{mt:1.5,py:.5},children:e.jsx(l,{variant:"body2",children:s("endpoints.availableTools")})})]})}function me({tenantSlug:n}){const s=v(),[o,t]=T.useState(()=>{const p=window.location.origin;return p==="http://localhost:3000"?"https://localhost:3443":p});function r(){const p=xe(o),d=new Blob([p],{type:"application/xml"}),m=URL.createObjectURL(d),x=document.createElement("a");x.href=m,x.download="manifest.xml",x.click(),URL.revokeObjectURL(m)}return e.jsxs(i,{children:[e.jsx(b,{severity:"success",sx:{mb:1.5},children:s("endpoints.excelPluginAlert")}),e.jsxs(i,{sx:{mb:1.5},children:[e.jsx(l,{variant:"body2",color:"text.secondary",sx:{mb:.75},children:s("endpoints.deployedManifestInfo")}),e.jsx(B,{variant:"contained",size:"small",startIcon:e.jsx(k,{}),onClick:r,sx:{textTransform:"none",mr:1.5},children:s("endpoints.downloadDeployedManifest")}),e.jsxs(L,{href:"/help/excel-plugin.html",target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5},children:[e.jsx(E,{sx:{fontSize:16}}),s("endpoints.setupGuide")]})]}),e.jsx(l,{variant:"body2",color:"text.secondary",sx:{mb:.75},children:s("endpoints.customManifestInfo")}),e.jsx(ne,{label:s("endpoints.serverUrlLabel"),size:"small",fullWidth:!0,value:o,onChange:p=>t(p.target.value),helperText:s("endpoints.serverUrlHelperText"),InputLabelProps:{shrink:!0},sx:{mb:1}}),e.jsx(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:e.jsx(c,{label:`${s("endpoints.tenant")}: ${n}`,size:"small",variant:"outlined",sx:{borderColor:a.green,color:a.green}})}),e.jsx(B,{variant:"outlined",size:"small",startIcon:e.jsx(k,{}),onClick:r,sx:{textTransform:"none",mr:1.5},children:s("endpoints.downloadManifest")}),e.jsxs(b,{severity:"info",sx:{mt:2,py:.5},children:[e.jsx(l,{variant:"body2",fontWeight:600,sx:{mb:.25},children:s("endpoints.deploymentOptions")}),e.jsx(l,{variant:"body2",component:"div",children:s("endpoints.deploymentOptionsText")})]})]})}function Te(){var $,C;const n=v(),{projectId:s,modelId:o}=ee(),t=J(s,o),r=Q(s),p=z("tenant_id",""),d=(($=t.data)==null?void 0:$.slug)??o??"",m=((C=r.data)==null?void 0:C.slug)??"",x={modelId:o,modelSlug:d,tenantSlug:p,projectSlug:m},[u,y]=T.useState("rest");function f(O){y(Y=>Y===O?!1:O)}return e.jsxs(i,{sx:{p:2},children:[e.jsx(l,{variant:"subtitle2",fontWeight:700,sx:{mb:.5},children:n("panels.endpoints")}),e.jsx(l,{variant:"body2",color:"text.secondary",sx:{mb:2},children:n("endpoints.description")}),e.jsx(b,{severity:"info",sx:{mb:2,py:.5},children:n("endpoints.authenticationInfo")}),e.jsxs(b,{severity:"success",sx:{mb:2,py:.5},children:[e.jsx(l,{variant:"body2",fontWeight:600,sx:{mb:.25},children:n("endpoints.modelExposedAsTwo")}),e.jsx(l,{variant:"body2",component:"div",children:n("endpoints.modelViewsInfo",{modelSlug:d})}),e.jsx(l,{variant:"caption",color:"text.secondary",children:n("endpoints.sameConnectionString")})]}),e.jsxs(I,{expanded:u==="rest",onChange:()=>f("rest"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx(_,{expandIcon:e.jsx(w,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(l,{variant:"body2",fontWeight:600,children:n("endpoints.restApiTitle")}),e.jsx(c,{label:n("endpoints.httpJsonLabel"),size:"small",sx:{bgcolor:a.greenBg,color:a.green,fontWeight:500}})]})}),e.jsx(D,{sx:{pt:0},children:e.jsx(ce,{...x})})]}),e.jsxs(I,{expanded:u==="jdbc",onChange:()=>f("jdbc"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx(_,{expandIcon:e.jsx(w,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(l,{variant:"body2",fontWeight:600,children:n("endpoints.jdbcTitle")}),e.jsx(c,{label:`${n("endpoints.port")} ${j}`,size:"small",sx:{bgcolor:a.purpleBg,color:a.purple,fontWeight:500}})]})}),e.jsx(D,{sx:{pt:0},children:e.jsx(de,{...x})})]}),e.jsxs(I,{expanded:u==="xmla",onChange:()=>f("xmla"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx(_,{expandIcon:e.jsx(w,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(l,{variant:"body2",fontWeight:600,children:n("endpoints.xmlaTitle")}),e.jsx(c,{label:n("endpoints.powerBiExcelLabel"),size:"small",color:"success"})]})}),e.jsx(D,{sx:{pt:0},children:e.jsx(pe,{...x})})]}),e.jsxs(I,{expanded:u==="excel",onChange:()=>f("excel"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider","&:before":{display:"none"}},children:[e.jsx(_,{expandIcon:e.jsx(w,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(l,{variant:"body2",fontWeight:600,children:n("endpoints.excelPluginTitle")}),e.jsx(c,{label:n("endpoints.taskPaneAddinLabel"),size:"small",sx:{bgcolor:a.greenBg,color:a.green,fontWeight:500}})]})}),e.jsx(D,{sx:{pt:0},children:e.jsx(me,{...x})})]}),e.jsxs(I,{expanded:u==="mcp",onChange:()=>f("mcp"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx(_,{expandIcon:e.jsx(w,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(l,{variant:"body2",fontWeight:600,children:n("endpoints.mcpServerTitle")}),e.jsx(c,{label:n("endpoints.aiAssistantsLabel"),size:"small",sx:{bgcolor:a.purpleBg,color:a.purple,fontWeight:500}})]})}),e.jsx(D,{sx:{pt:0},children:e.jsx(ue,{...x})})]}),e.jsxs(i,{sx:{mt:2,display:"flex",gap:2,flexWrap:"wrap"},children:[e.jsxs(L,{href:"/api/v1/docs",target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5},children:[e.jsx(E,{sx:{fontSize:16}}),n("endpoints.modelServiceApiDocs")]}),e.jsxs(L,{href:`${R.replace(/\/api\/v1$/,"")}/docs`,target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5},children:[e.jsx(E,{sx:{fontSize:16}}),n("endpoints.queryRouterApiDocs")]})]})]})}export{Te as default,xe as generateManifest};
