import { useState } from "react";
import { Box, Typography } from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import ConnectionDialog from "../../Panels/ConnectionDialog";
import { connectionsApi } from "../../../api/client";
import type { ConnectionCreate } from "../../../api/types";
import { useT } from "../../../i18n";
import {
  CONN_FIELDS,
  buildJsonFromFields,
  getDefaultValues,
  isFieldVisible,
} from "../../connectionFields";

interface Props {
  projectId: string | null;
  onCreated: () => void;
}

export default function ConnectionStep({ projectId, onCreated }: Props) {
  const t = useT();
  const [done, setDone] = useState(false);
  const qc = useQueryClient();

  const [name, setName] = useState("");
  const [connType, setConnType] =
    useState<ConnectionCreate["connection_type"]>("postgresql");
  const [fields, setFields] = useState<Record<string, string>>(
    getDefaultValues(CONN_FIELDS["postgresql"] ?? []),
  );
  const [testResult, setTestResult] = useState<string | null>(null);

  function getPayload() {
    const defs = CONN_FIELDS[connType] ?? [];
    return {
      display_name: name,
      connection_type: connType,
      credentials: buildJsonFromFields(defs, fields, "credentials"),
      config: buildJsonFromFields(defs, fields, "config"),
    };
  }

  const create = useMutation({
    mutationFn: () => connectionsApi.create(projectId!, getPayload()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["connections", projectId] });
      setDone(true);
      onCreated();
    },
  });

  const testDraft = useMutation({
    mutationFn: () => connectionsApi.testDraft(projectId!, getPayload()),
    onSuccess: (result) =>
      setTestResult(result.ok ? "success" : result.detail ?? "failed"),
    onError: (error: unknown) =>
      setTestResult(error instanceof Error ? error.message : "failed"),
  });

  if (!projectId) {
    return (
      <Box>
        <Typography variant="body1" color="text.secondary">
          {t("wizardConn.selectProjectFirst")}
        </Typography>
      </Box>
    );
  }

  if (done) {
    return (
      <Box>
        <Typography variant="body1" color="success.main">
          {t("wizardConn.connectionCreated")}
        </Typography>
      </Box>
    );
  }

  return (
    <Box>
      <Typography variant="body1" paragraph>
        {t("wizardConn.intro")}
      </Typography>
      <ConnectionDialog
        open
        mode="create"
        name={name}
        onNameChange={setName}
        connType={connType}
        onConnTypeChange={(ct) => {
          setConnType(ct);
          setFields(getDefaultValues(CONN_FIELDS[ct] ?? []));
          setTestResult(null);
        }}
        fields={fields}
        onFieldChange={(k, v) => setFields((prev) => ({ ...prev, [k]: v }))}
        testResult={testResult}
        isError={testResult !== null && testResult !== "success"}
        isSaving={create.isPending}
        isTesting={testDraft.isPending}
        onTest={() => testDraft.mutate()}
        onSave={() => create.mutate()}
        onClose={() => {
          setDone(true);
          onCreated();
        }}
      />
    </Box>
  );
}
