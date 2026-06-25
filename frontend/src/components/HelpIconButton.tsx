import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { IconButton, Tooltip, type SxProps, type Theme } from "@mui/material";
import { useT } from "../i18n";

interface Props {
  href: string;
  title?: string;
  sx?: SxProps<Theme>;
}

export default function HelpIconButton({ href, title, sx }: Props) {
  const t = useT();
  const resolvedTitle = title ?? t("help.defaultTitle");
  return (
    <Tooltip title={resolvedTitle}>
      <IconButton
        aria-label={resolvedTitle}
        component="a"
        href={href}
        rel="noopener"
        size="small"
        sx={sx}
        target="_blank"
      >
        <InfoOutlinedIcon fontSize="small" />
      </IconButton>
    </Tooltip>
  );
}
