import{r as f,j as e}from"./react-vendor-GPNyayNe.js";import{u as y,S as Y,s as B,ak as c,ax as J}from"./index-YhOwlAm4.js";import{a5 as T,b9 as U,n as W,o as G,aC as O}from"./mui-icons-C20jrUWQ.js";import{b as X}from"./react-router-Bvj3z2RT.js";import{B as i,d as a,h as I,aa as S,ab as $,C as x,ac as w,u as A,H as E,J as b,T as M,I as V,o as R,m as N}from"./mui-core-C_h8030x.js";import"./vendor-B2zYrXBr.js";import"./react-query-FQ2-Hu80.js";function Q(){return typeof window>"u"?"localhost":window.location.hostname||"localhost"}function K(){return typeof window>"u"?"http:":window.location.protocol||"http:"}function L(s,n,o){const t=typeof window<"u"?B(s,""):"";return t.trim()?t.trim():n??o}const D=Q(),z=K(),P=J().endpointDefaults,H=L("builder.settings.queryRouterUrl",void 0,`${z}//${D}:${P.query_router_port}`),k=L("builder.settings.gatewayHttpUrl",void 0,`${z}//${D}:${P.gateway_http_port}`),C=L("builder.settings.gatewayJdbcHost",void 0,D),v=L("builder.settings.gatewayJdbcPort",void 0,String(P.gateway_jdbc_port));function g({code:s}){const n=y(),[o,t]=f.useState(!1);function r(){navigator.clipboard.writeText(s).then(()=>{t(!0),setTimeout(()=>t(!1),1800)})}return e.jsxs(i,{sx:{position:"relative"},children:[e.jsx(i,{component:"pre",sx:{m:0,p:1.5,pr:5,bgcolor:"grey.900",color:"grey.100",borderRadius:1,fontSize:"0.72rem",fontFamily:"monospace",whiteSpace:"pre-wrap",wordBreak:"break-all",overflowX:"auto"},children:s}),e.jsx(M,{title:n(o?"endpoints.copiedToClipboard":"common.copy"),children:e.jsx(V,{size:"small",onClick:r,sx:{position:"absolute",top:4,right:4,color:o?"success.light":"grey.400","&:hover":{color:"grey.100"}},children:o?e.jsx(W,{fontSize:"small"}):e.jsx(G,{fontSize:"small"})})})]})}function Z({modelId:s,modelSlug:n,tenantSlug:o}){const t=y(),[r,m]=f.useState(0),l=`${H}/api/v1`,p=`curl -X POST "${l}/execute" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <your_token>" \\
  -d '{
    "model_id": "${s}",
    "raw_query": "SELECT region, SUM(revenue) FROM ${n} GROUP BY region",
    "protocol": "jdbc"
  }'`,u=`import requests

response = requests.post(
    "${l}/execute",
    headers={
        "Authorization": "Bearer <your_token>",
        "Content-Type": "application/json",
    },
    json={
        "model_id": "${s}",
        "raw_query": "SELECT region, SUM(revenue) FROM ${n} GROUP BY region",
        "protocol": "jdbc",
    },
)
data = response.json()
print(data["rows"])`,d=`curl -X POST "${l}/explain" \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <your_token>" \\
  -d '{
    "model_id": "${s}",
    "raw_query": "SELECT region, SUM(revenue) FROM ${n} GROUP BY region",
    "protocol": "jdbc"
  }'`;return e.jsxs(i,{children:[e.jsx(a,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:t("endpoints.restApiDescription")}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(x,{label:t("endpoints.runQuery"),size:"small",variant:"outlined",sx:{borderColor:c.green,color:c.green}}),e.jsx(x,{label:t("endpoints.explainRoute"),size:"small",color:"default",variant:"outlined"})]}),e.jsxs(E,{value:r,onChange:(j,h)=>m(h),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(b,{label:t("endpoints.curlTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(b,{label:t("endpoints.pythonTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(b,{label:t("endpoints.explain"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),r===0&&e.jsx(g,{code:p}),r===1&&e.jsx(g,{code:u}),r===2&&e.jsx(g,{code:d})]})}function ee({modelId:s,modelSlug:n,tenantSlug:o}){const t=y(),[r,m]=f.useState(0),l=`jdbc:postgresql://${C}:${v}/${o}?model_id=${s}`,p=`psql "host=${C} port=${v} dbname=${o} user=<your_email> password=<your_password> sslmode=prefer"

# Query using the model slug as the table name.
# The gateway resolves the model automatically.
SELECT region, SUM(revenue) FROM ${n} GROUP BY region`,u=`import psycopg2

conn = psycopg2.connect(
    host="${C}",
    port=${v},
    dbname="${o}",
    user="<your_email>",
    password="<your_password>",
    options="-c model_id=${s}",
)
cur = conn.cursor()
cur.execute("SELECT region, SUM(revenue) FROM ${n} GROUP BY region")
print(cur.fetchall())`,d=`// Maven: org.postgresql:postgresql:42.7.3
String url = "${l}";
Properties props = new Properties();
props.setProperty("user", "<your_email>");
props.setProperty("password", "<your_password>");

try (Connection conn = DriverManager.getConnection(url, props);
     Statement stmt = conn.createStatement()) {
    ResultSet rs = stmt.executeQuery(
        "SELECT region, SUM(revenue) FROM ${n} GROUP BY region"
    );
    while (rs.next()) System.out.println(rs.getString(1) + " " + rs.getLong(2));
}`;return e.jsxs(i,{children:[e.jsx(a,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:t("endpoints.jdbcDescription",{port:v})}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(x,{label:`${t("endpoints.host")}: ${C}`,size:"small",variant:"outlined"}),e.jsx(x,{label:`${t("endpoints.port")}: ${v}`,size:"small",variant:"outlined"}),e.jsx(x,{label:`${t("endpoints.database")}: ${o}`,size:"small",variant:"outlined",sx:{borderColor:c.green,color:c.green}})]}),e.jsxs(E,{value:r,onChange:(j,h)=>m(h),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(b,{label:t("endpoints.psqlTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(b,{label:t("endpoints.pythonTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(b,{label:t("endpoints.javaTab"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),r===0&&e.jsx(g,{code:p}),r===1&&e.jsx(g,{code:u}),r===2&&e.jsx(g,{code:d})]})}function ne({modelSlug:s,tenantSlug:n}){const o=y(),t=`${k}/api/v1/xmla`,r=`${k}/api/v1/xmla/${n}`,m=`curl -X POST "${r}" \\
  -H "Content-Type: text/xml" \\
  -H "Authorization: Basic $(echo -n '<your_email>:<your_password>' | base64)" \\
  -d '<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">
  <Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>DBSCHEMA_CATALOGS</RequestType>
      <Properties>
        <PropertyList>
          <Catalog>${n}</Catalog>
        </PropertyList>
      </Properties>
    </Discover>
  </Body>
</Envelope>'`,l=`1. Open Power BI Desktop
2. Get Data → Analysis Services
3. Server: ${r}
4. Authentication: Basic
   Username: <your_email>
   Password: <your_password>
5. Select cube/model: ${s}`,p=`1. Data → Get Data → From Database → From Analysis Services
2. Server name: ${t}
3. Log on credentials: Use the following...
   User name: <your_email>
   Password: <your_password>
4. Select catalog/database: ${n}
5. Select model/cube: ${s}`,u=`Provider=MSOLAP;Data Source=${r};Catalog=${n};`,[d,j]=f.useState(0),[h,_]=f.useState(!1);function q(){navigator.clipboard.writeText(u).then(()=>{_(!0),setTimeout(()=>_(!1),1800)})}return e.jsxs(i,{children:[e.jsx(a,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:o("endpoints.xmlaDescription")}),e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1,mb:1.5},children:[e.jsx(a,{variant:"caption",color:"text.secondary",children:o("endpoints.connectionString")}),e.jsx(i,{component:"code",sx:{flex:1,fontSize:"0.7rem",fontFamily:"monospace",bgcolor:"grey.900",color:"grey.100",px:1,py:.5,borderRadius:.5,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"},children:u}),e.jsx(M,{title:o(h?"endpoints.copiedToClipboard":"endpoints.copyConnectionString"),children:e.jsx(V,{size:"small",onClick:q,children:h?e.jsx(W,{fontSize:"small",color:"success"}):e.jsx(G,{fontSize:"small"})})})]}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(x,{label:`${o("endpoints.tenantCatalog")}: ${n||"<tenant_slug>"}`,size:"small",variant:"outlined",sx:{borderColor:c.green,color:c.green}}),e.jsx(x,{label:`${o("endpoints.model")}: ${s}`,size:"small",variant:"outlined"}),e.jsx(x,{label:o("endpoints.soapXmlaLabel"),size:"small",variant:"outlined"})]}),e.jsxs(E,{value:d,onChange:(ie,F)=>j(F),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(b,{label:o("endpoints.powerBiTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(b,{label:o("endpoints.excelTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(b,{label:o("endpoints.soapCurlTab"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),d===0&&e.jsx(g,{code:l}),d===1&&e.jsx(g,{code:p}),d===2&&e.jsx(g,{code:m})]})}function se(s){const n=s.replace(/\/$/,"");return`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
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

  <IconUrl DefaultValue="${n}/excel-plugin/assets/icon-32.png"/>
  <HighResolutionIconUrl DefaultValue="${n}/excel-plugin/assets/icon-80.png"/>

  <SupportUrl DefaultValue="${n}/help/excel-plugin.html"/>

  <AppDomains>
    <AppDomain>${n}</AppDomain>
  </AppDomains>

  <Hosts>
    <Host Name="Workbook"/>
  </Hosts>

  <DefaultSettings>
    <SourceLocation DefaultValue="${n}/excel-plugin/index.html"/>
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
        <bt:Image id="Icon16" DefaultValue="${n}/excel-plugin/assets/icon-16.png"/>
        <bt:Image id="Icon32" DefaultValue="${n}/excel-plugin/assets/icon-32.png"/>
        <bt:Image id="Icon80" DefaultValue="${n}/excel-plugin/assets/icon-80.png"/>
      </bt:Images>
      <bt:Urls>
        <bt:Url id="Taskpane.Url" DefaultValue="${n}/excel-plugin/index.html"/>
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
</OfficeApp>`}function oe({tenantSlug:s}){const n=y(),o=`${z}//${D}:${P.model_service_port}`,t=H.replace(/\/api\/v1$/,""),r=`{
  "mcpServers": {
    "tessallite": {
      "command": "tessallite-mcp",
      "env": {
        "TESSALLITE_URL": "${o}",
        "TESSALLITE_QUERY_URL": "${t}",
        "TESSALLITE_TENANT_ID": "${s}",
        "TESSALLITE_EMAIL": "<your_email>",
        "TESSALLITE_PASSWORD": "<your_password>"
      }
    }
  }
}`,m=`pip install tessallite-mcp
# or from the monorepo:
cd tessallite/mcp-server && pip install -e .`,[l,p]=f.useState(0);return e.jsxs(i,{children:[e.jsx(a,{variant:"body2",color:"text.secondary",sx:{mb:1.5},children:n("endpoints.mcpDescription")}),e.jsxs(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:[e.jsx(x,{label:`${n("endpoints.tenant")}: ${s}`,size:"small",variant:"outlined",sx:{borderColor:c.green,color:c.green}}),e.jsx(x,{label:n("endpoints.stdioTransport"),size:"small",variant:"outlined"})]}),e.jsxs(E,{value:l,onChange:(u,d)=>p(d),sx:{mb:1,minHeight:32},TabIndicatorProps:{style:{height:2}},children:[e.jsx(b,{label:n("endpoints.claudeDesktopTab"),sx:{minHeight:32,py:0,textTransform:"none"}}),e.jsx(b,{label:n("endpoints.install"),sx:{minHeight:32,py:0,textTransform:"none"}})]}),l===0&&e.jsx(g,{code:r}),l===1&&e.jsx(g,{code:m}),e.jsx(I,{severity:"info",sx:{mt:1.5,py:.5},children:e.jsx(a,{variant:"body2",children:n("endpoints.availableTools")})})]})}function te({tenantSlug:s}){const n=y(),[o,t]=f.useState(()=>window.location.origin);function r(){const m=se(o),l=new Blob([m],{type:"application/xml"}),p=URL.createObjectURL(l),u=document.createElement("a");u.href=p,u.download="manifest.xml",u.click(),URL.revokeObjectURL(p)}return e.jsxs(i,{children:[e.jsx(I,{severity:"success",sx:{mb:1.5},children:n("endpoints.excelPluginAlert")}),e.jsxs(i,{sx:{mb:1.5},children:[e.jsx(a,{variant:"body2",color:"text.secondary",sx:{mb:.75},children:n("endpoints.deployedManifestInfo")}),e.jsx(R,{variant:"contained",size:"small",startIcon:e.jsx(O,{}),component:"a",href:"/excel-plugin/manifest.xml",download:"manifest.xml",sx:{textTransform:"none",mr:1.5},children:n("endpoints.downloadDeployedManifest")}),e.jsxs(A,{href:"/help/excel-plugin.html",target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5},children:[e.jsx(U,{sx:{fontSize:16}}),n("endpoints.setupGuide")]})]}),e.jsx(a,{variant:"body2",color:"text.secondary",sx:{mb:.75},children:n("endpoints.customManifestInfo")}),e.jsx(N,{label:n("endpoints.serverUrlLabel"),size:"small",fullWidth:!0,value:o,onChange:m=>t(m.target.value),helperText:n("endpoints.serverUrlHelperText"),InputLabelProps:{shrink:!0},sx:{mb:1}}),e.jsx(i,{sx:{display:"flex",gap:1,mb:1.5,flexWrap:"wrap"},children:e.jsx(x,{label:`${n("endpoints.tenant")}: ${s}`,size:"small",variant:"outlined",sx:{borderColor:c.green,color:c.green}})}),e.jsx(R,{variant:"outlined",size:"small",startIcon:e.jsx(O,{}),onClick:r,sx:{textTransform:"none",mr:1.5},children:n("endpoints.downloadManifest")}),e.jsxs(I,{severity:"info",sx:{mt:2,py:.5},children:[e.jsx(a,{variant:"body2",fontWeight:600,sx:{mb:.25},children:n("endpoints.deploymentOptions")}),e.jsx(a,{variant:"body2",component:"div",children:n("endpoints.deploymentOptionsText")})]})]})}function me(){var j;const s=y(),{projectId:n,modelId:o}=X(),t=Y(n,o),r=B("tenant_id",""),m=((j=t.data)==null?void 0:j.slug)??o??"",l={modelId:o,modelSlug:m,tenantSlug:r},[p,u]=f.useState("rest");function d(h){u(_=>_===h?!1:h)}return e.jsxs(i,{sx:{p:2},children:[e.jsx(a,{variant:"subtitle2",fontWeight:700,sx:{mb:.5},children:s("panels.endpoints")}),e.jsx(a,{variant:"body2",color:"text.secondary",sx:{mb:2},children:s("endpoints.description")}),e.jsx(I,{severity:"info",sx:{mb:2,py:.5},children:s("endpoints.authenticationInfo")}),e.jsxs(I,{severity:"success",sx:{mb:2,py:.5},children:[e.jsx(a,{variant:"body2",fontWeight:600,sx:{mb:.25},children:s("endpoints.modelExposedAsTwo")}),e.jsx(a,{variant:"body2",component:"div",children:s("endpoints.modelViewsInfo",{modelSlug:m})}),e.jsx(a,{variant:"caption",color:"text.secondary",children:s("endpoints.sameConnectionString")})]}),e.jsxs(S,{expanded:p==="rest",onChange:()=>d("rest"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx($,{expandIcon:e.jsx(T,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(a,{variant:"body2",fontWeight:600,children:s("endpoints.restApiTitle")}),e.jsx(x,{label:s("endpoints.httpJsonLabel"),size:"small",sx:{bgcolor:c.greenBg,color:c.green,fontWeight:500}})]})}),e.jsx(w,{sx:{pt:0},children:e.jsx(Z,{...l})})]}),e.jsxs(S,{expanded:p==="jdbc",onChange:()=>d("jdbc"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx($,{expandIcon:e.jsx(T,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(a,{variant:"body2",fontWeight:600,children:s("endpoints.jdbcTitle")}),e.jsx(x,{label:`${s("endpoints.port")} ${v}`,size:"small",sx:{bgcolor:c.purpleBg,color:c.purple,fontWeight:500}})]})}),e.jsx(w,{sx:{pt:0},children:e.jsx(ee,{...l})})]}),e.jsxs(S,{expanded:p==="xmla",onChange:()=>d("xmla"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx($,{expandIcon:e.jsx(T,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(a,{variant:"body2",fontWeight:600,children:s("endpoints.xmlaTitle")}),e.jsx(x,{label:s("endpoints.powerBiExcelLabel"),size:"small",color:"success"})]})}),e.jsx(w,{sx:{pt:0},children:e.jsx(ne,{...l})})]}),e.jsxs(S,{expanded:p==="excel",onChange:()=>d("excel"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider","&:before":{display:"none"}},children:[e.jsx($,{expandIcon:e.jsx(T,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(a,{variant:"body2",fontWeight:600,children:s("endpoints.excelPluginTitle")}),e.jsx(x,{label:s("endpoints.taskPaneAddinLabel"),size:"small",sx:{bgcolor:c.greenBg,color:c.green,fontWeight:500}})]})}),e.jsx(w,{sx:{pt:0},children:e.jsx(te,{...l})})]}),e.jsxs(S,{expanded:p==="mcp",onChange:()=>d("mcp"),disableGutters:!0,elevation:0,sx:{border:1,borderColor:"divider",mb:1,"&:before":{display:"none"}},children:[e.jsx($,{expandIcon:e.jsx(T,{}),sx:{minHeight:40},children:e.jsxs(i,{sx:{display:"flex",alignItems:"center",gap:1},children:[e.jsx(a,{variant:"body2",fontWeight:600,children:s("endpoints.mcpServerTitle")}),e.jsx(x,{label:s("endpoints.aiAssistantsLabel"),size:"small",sx:{bgcolor:c.purpleBg,color:c.purple,fontWeight:500}})]})}),e.jsx(w,{sx:{pt:0},children:e.jsx(oe,{...l})})]}),e.jsxs(i,{sx:{mt:2,display:"flex",gap:2,flexWrap:"wrap"},children:[e.jsxs(A,{href:"/api/v1/docs",target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5},children:[e.jsx(U,{sx:{fontSize:16}}),s("endpoints.modelServiceApiDocs")]}),e.jsxs(A,{href:`${H.replace(/\/api\/v1$/,"")}/docs`,target:"_blank",rel:"noopener",sx:{fontSize:"0.8rem",display:"inline-flex",alignItems:"center",gap:.5},children:[e.jsx(U,{sx:{fontSize:16}}),s("endpoints.queryRouterApiDocs")]})]})]})}export{me as default};
