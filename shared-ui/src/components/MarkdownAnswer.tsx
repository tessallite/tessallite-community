import React, { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Box } from "@mui/material";

interface MarkdownAnswerProps {
  content: string;
}

export function MarkdownAnswer({ content }: MarkdownAnswerProps) {
  const displayContent = useMemo(() => normalizePlainNarration(content), [content]);
  const components = useMemo(
    () => ({
      a: ({
        href,
        children,
        ...props
      }: React.AnchorHTMLAttributes<HTMLAnchorElement> & {
        children?: React.ReactNode;
      }) => (
        <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
          {children}
        </a>
      ),
      table: ({
        children,
        ...props
      }: React.HTMLAttributes<HTMLTableElement> & {
        children?: React.ReactNode;
      }) => (
        <Box sx={{ overflowX: "auto", my: 1 }}>
          <table {...props}>{children}</table>
        </Box>
      ),
      pre: ({
        children,
        ...props
      }: React.HTMLAttributes<HTMLPreElement> & {
        children?: React.ReactNode;
      }) => (
        <Box
          component="pre"
          sx={{
            bgcolor: "action.hover",
            p: 1.5,
            borderRadius: 1,
            overflowX: "auto",
            maxWidth: "100%",
            fontSize: 13,
            fontFamily:
              '"JetBrains Mono", "SFMono-Regular", "Consolas", monospace',
          }}
          {...props}
        >
          {children}
        </Box>
      ),
      code: ({
        className,
        children,
        ...props
      }: React.HTMLAttributes<HTMLElement> & {
        children?: React.ReactNode;
      }) => {
        const isBlock = className?.includes("language-");
        if (isBlock) {
          return (
            <code className={className} {...props}>
              {children}
            </code>
          );
        }
        return (
          <Box
            component="code"
            sx={{
              bgcolor: "action.hover",
              px: 0.5,
              py: 0.15,
              borderRadius: 0.5,
              fontSize: 13,
              fontFamily:
                '"JetBrains Mono", "SFMono-Regular", "Consolas", monospace',
            }}
            {...props}
          >
            {children}
          </Box>
        );
      },
    }),
    [],
  );

  return (
    <Box
      sx={{
        fontSize: 15,
        lineHeight: 1.6,
        minWidth: 0,
        maxWidth: "100%",
        overflowWrap: "anywhere",
        wordBreak: "break-word",
        "& h1, & h2, & h3, & h4, & h5, & h6": { mt: 1.5, mb: 0.5 },
        "& p": { my: 0.5 },
        "& ul, & ol": { my: 0.5, pl: 2 },
        "& li": { my: 0.25 },
        "& blockquote": {
          borderLeft: 3,
          borderColor: "divider",
          pl: 2,
          my: 1,
          color: "text.secondary",
        },
        "& hr": { borderColor: "divider", my: 2 },
        "& table": {
          borderCollapse: "collapse",
          width: "100%",
          fontSize: 13,
        },
        "& th, & td": {
          border: 1,
          borderColor: "divider",
          px: 1,
          py: 0.5,
          textAlign: "left",
        },
        "& th": { bgcolor: "action.hover", fontWeight: 600 },
      }}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={components}
        allowedElements={undefined}
      >
        {displayContent}
      </ReactMarkdown>
    </Box>
  );
}

function normalizePlainNarration(content: string): string {
  const trimmed = content.trim();
  if (!trimmed) return content;
  if (/(\n|^#{1,6}\s|^\s*[-*]\s|\|.+\|)/m.test(trimmed)) return content;
  if (trimmed.length < 360) return content;
  const sentences = trimmed.match(/[^.!?]+[.!?]+(?:\s+|$)/g);
  if (!sentences || sentences.length < 4) return content;
  const paragraphs: string[] = [];
  for (let i = 0; i < sentences.length; i += 2) {
    paragraphs.push(sentences.slice(i, i + 2).join(" ").replace(/\s+/g, " ").trim());
  }
  const covered = sentences.join("").length;
  const tail = trimmed.slice(covered).trim();
  if (tail) paragraphs.push(tail);
  return paragraphs.join("\n\n");
}
