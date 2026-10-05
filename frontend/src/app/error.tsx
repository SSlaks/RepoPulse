"use client";

import { RotateCcw } from "lucide-react";

export default function GlobalError({ retry }: { error: Error; retry: () => void }) {
  return (
    <main className="full-page-state shell">
      <span className="state-code">500</span>
      <h1>页面暂时没有响应</h1>
      <p>数据服务暂时不可用或请求超时，请稍后重试。</p>
      <button className="primary-button" type="button" onClick={retry}><RotateCcw size={17} />重新加载</button>
    </main>
  );
}

