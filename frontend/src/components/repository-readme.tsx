import { ArrowUpRight, FileText } from "lucide-react";
import { headers } from "next/headers";
import { ReadmeAi } from "@/components/readme-ai";
import { fetchReadme } from "@/lib/api";
import { copyTrustedProxyHeaders } from "@/lib/trusted-proxy";

import type { ReadmeResponse } from "@/lib/types";

interface RepositoryReadmeProps {
  owner: string;
  name: string;
}

export async function RepositoryReadme({ owner, name }: RepositoryReadmeProps) {
  let readme: ReadmeResponse | null = null;
  try {
    readme = await fetchReadme(owner, name, copyTrustedProxyHeaders(await headers()));
  } catch {
    // Keep the existing empty state when GitHub is unavailable.
  }

  return (
    <section className="readme-section" aria-labelledby="readme-heading">
      <ReadmeHeading readme={readme} />
      {readme ? (
        <ReadmeAi
          key={readme.repository + readme.content}
          markdown={readme.content}
          imageBaseUrl={readme.html_url}
          repository={readme.repository}
        />
      ) : (
        <div className="readme-empty">
          <FileText size={22} />
          <strong>README 暂不可用</strong>
          <p>GitHub 暂未提供可读取的 README 内容。</p>
        </div>
      )}
    </section>
  );
}

export function RepositoryReadmeFallback() {
  return (
    <section className="readme-section" aria-labelledby="readme-heading" aria-busy="true">
      <ReadmeHeading />
      <div className="readme-loading" role="status">正在加载 README</div>
    </section>
  );
}

function ReadmeHeading({ readme }: { readme?: ReadmeResponse | null }) {
  return (
    <div className="readme-heading">
      <div className="readme-heading-copy">
        <span className="readme-heading-icon" aria-hidden="true">
          <FileText size={19} />
        </span>
        <div>
          <h2 id="readme-heading">项目文档</h2>
          <p>仓库 README</p>
        </div>
      </div>
      {readme ? (
        <a
          className="readme-source"
          href={readme.html_url}
          target="_blank"
          rel="noreferrer"
          aria-label={`在 GitHub 查看 ${readme.path}`}
          title={readme.path}
        >
          <span className="readme-source-path">{readme.path}</span>
          <ArrowUpRight size={14} aria-hidden="true" />
        </a>
      ) : null}
    </div>
  );
}
