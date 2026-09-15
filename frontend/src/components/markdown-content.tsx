import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

interface MarkdownContentProps {
  content: string;
  imageBaseUrl?: string;
}

export function MarkdownContent({ content, imageBaseUrl }: MarkdownContentProps) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]} components={{
    a: ({ children, href }) => {
      const resolvedHref = resolveMarkdownLinkUrl(typeof href === "string" ? href : undefined, imageBaseUrl);
      return resolvedHref ? <a href={resolvedHref} target="_blank" rel="noreferrer">{children}</a> : null;
    },
    img: ({ src, alt, ...props }) => {
      const resolvedSrc = resolveMarkdownImageUrl(typeof src === "string" ? src : undefined, imageBaseUrl);
      return resolvedSrc ? (
        // README 图片来自动态的多个远程域名，原生 img 才能保留其地址和尺寸语义。
        // eslint-disable-next-line @next/next/no-img-element
        <img
          {...props}
          src={resolvedSrc}
          alt={alt ?? ""}
          loading="lazy"
          decoding="async"
        />
      ) : null;
    },
  }}>{content}</ReactMarkdown>;
}

export function resolveMarkdownLinkUrl(href: string | undefined, baseUrl?: string): string | undefined {
  if (!href) return undefined;
  if (href.startsWith("#")) return href;
  try {
    const url = new URL(href, baseUrl);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function resolveMarkdownImageUrl(src: string | undefined, baseUrl?: string): string | undefined {
  if (!src) return undefined;
  try {
    const url = new URL(src, baseUrl);
    if (url.protocol === "data:") {
      return url.href.startsWith("data:image/") ? url.href : undefined;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return undefined;
    if (url.hostname === "github.com") {
      const blobMarker = "/blob/";
      const blobIndex = url.pathname.indexOf(blobMarker);
      if (blobIndex >= 0) {
        url.pathname = `${url.pathname.slice(0, blobIndex)}/raw/${url.pathname.slice(blobIndex + blobMarker.length)}`;
      }
    }
    return url.href;
  } catch {
    return undefined;
  }
}
