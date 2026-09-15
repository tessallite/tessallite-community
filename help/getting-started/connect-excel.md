---
title: "Connect Excel via XMLA"
audience: analyst
area: getting-started
updated: 2026-04-17
---

## What this covers

Connecting Microsoft Excel to a Tessallite workspace via the XMLA endpoint, selecting a model catalogue, and building a pivot table from the model's dimensions and measures.

---

## Before you start

- Role required: Analyst (Viewer) or higher.
- You will need: Microsoft Excel for Windows with the **Data** tab available, your email address, and your Tessallite password.
- You will also need the XMLA endpoint URL. The gateway serves XMLA using one
  scheme selected at startup by `GATEWAY_XMLA_TLS_ENABLED`:
  - `GATEWAY_XMLA_TLS_ENABLED=false` (the default in local Compose) serves
    plain HTTP, for example `http://localhost:8080/api/v1/xmla/`.
  - `GATEWAY_XMLA_TLS_ENABLED=true` serves TLS, for example
    `https://localhost:8080/api/v1/xmla/`; the gateway must have its TLS
    certificate and key configured.
  A reverse proxy may terminate TLS and publish an `https://` URL while the
  gateway listener behind it remains plain HTTP. The machine where Excel runs
  does not determine the scheme; the deployment setting and published URL do.
- The scheme matters. Excel sends your email address and password on this
  connection, so use `https://` whenever traffic leaves the machine. Use the
  exact scheme configured for the listener; a plain-HTTP listener does not
  answer `https://`, and a TLS listener does not answer `http://`.
- The trailing slash in the URL is required. The connection wizard does not accept the URL without it.
- Windows Authentication is not supported. You must enter your email address and password manually.

---

## Step 1: Open the connection wizard

1. Open Excel.
2. Click the **Data** tab in the ribbon.
3. Click **Get Data**.
4. From the menu, select **From Database**.
5. From the submenu, select **From Analysis Services**.
6. The Data Connection Wizard opens.

---

## Step 2: Enter the server address

1. In the **Server name** field, enter the XMLA endpoint URL using the scheme selected by `GATEWAY_XMLA_TLS_ENABLED` and the URL published by your administrator. For example, use `http://localhost:8080/api/v1/xmla/` when the setting is `false`, or `https://localhost:8080/api/v1/xmla/` when it is `true`.
2. Under **Log on credentials**, select **Use the following User Name and Password**.
3. In the **User Name** field, enter your email address.
4. In the **Password** field, enter your Tessallite password.
5. Click **Next**.

![Excel Data Connection Wizard — server name and credentials fields.](../assets/screencaps/connect-excel-wizard.png)

---

## Step 3: Select a catalogue

1. Excel connects to the gateway and retrieves a list of available catalogues.
2. Each model appears twice in the list:
   - The plain model name (e.g. `orders`) — this is the business view, with curated fields and calculated measures.
   - The model name with `_technical` appended (e.g. `orders_technical`) — this is the unfiltered view, intended for modellers and power users.
3. Select the catalogue you want to connect to. For most purposes, select the plain name.
4. Click **Next**, then click **Finish**.

![Excel Data Connection Wizard — catalogue selection showing model and persona catalogues.](../assets/screencaps/connect-excel-catalogue.png)

---

## Step 4: Build a pivot table

1. Excel asks where to place the data. Select a cell in your worksheet and click **OK**.
2. The PivotTable Field List opens on the right side. It shows the dimensions and measures from the selected model.
3. Drag dimension fields to the **Rows** or **Columns** area.
4. Drag measure fields to the **Values** area.
5. Excel queries Tessallite and populates the pivot table with results.

![Excel XMLA connection sequence.](../assets/illustrations/excel-connection-flow.svg)

---

## Refreshing the data

Right-click anywhere in the pivot table and select **Refresh** to retrieve the latest data from Tessallite.

---

## Tenant-specific URL

If your system administrator has set up per-tenant XMLA endpoints, the URL format is:

`https://<hostname>:8080/api/v1/xmla/<workspace-slug>`

Replace `<hostname>` with the gateway host and `<workspace-slug>` with your
workspace identifier. This form does not require a trailing slash.

Use the scheme selected by `GATEWAY_XMLA_TLS_ENABLED` for a directly exposed
gateway. If a load balancer or reverse proxy terminates TLS, use its published
`https://` URL even when the gateway's internal listener uses plain HTTP. Do
not infer the scheme from whether `<hostname>` is `localhost`; ask your system
administrator for the published URL. The wrong scheme either fails to connect
or, for a remote plain-HTTP connection, sends your password in the clear.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| "Unable to connect to data source" | Wrong URL format or gateway not running | Confirm the URL includes the trailing slash. Confirm the gateway service is running. |
| "Unable to connect" against a host that is running | Wrong scheme for how the gateway is published | Check `GATEWAY_XMLA_TLS_ENABLED` and any reverse-proxy configuration. A TLS listener does not answer `http://`, and a plain-HTTP listener does not answer `https://`. Use the published URL from your system administrator. |
| No catalogues appear after entering credentials | Authentication failed | Re-enter your email address and password. Do not select Windows Authentication. |
| Pivot table shows no data | No data in the model's source | Verify with the modeller that the source connection is active and data is present. |
| Pivot table figures appear incorrect | Wrong catalogue selected | Disconnect and reconnect using the plain catalogue name (business view), not the `_technical` variant. |

---

## Related

- [Excel XMLA connection guide](../integrations/excel-xmla-connection-guide.md)
- [Excel connection problems](../troubleshooting/excel-connection-problems.md)
- [Connect a BI tool via JDBC](connect-a-bi-tool.md)

---

← [Connect a BI Tool via JDBC](connect-a-bi-tool.md) | [Home](../index.md) | [Tessallite Features →](tessallite-features.md)
