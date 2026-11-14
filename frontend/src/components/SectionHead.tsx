import type { ReactNode } from "react";

type SectionHeadProps = {
  title: string;
  id: string;
  children?: ReactNode;
};

export function SectionHead({ title, id, children }: SectionHeadProps) {
  return (
    <header className="sec-head">
      <h2 id={id}>{title}</h2>
      {children && <div className="sec-meta">{children}</div>}
    </header>
  );
}
