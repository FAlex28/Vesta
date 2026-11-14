import { Shield } from "lucide-react";

export function BrandLockup({ caption = "Siguranță personală" }: { caption?: string }) {
  return (
    <div className="brand-lockup">
      <div className="brand-mark" aria-hidden="true"><Shield size={16} strokeWidth={2.4} /></div>
      <div className="brand-name">vesta</div>
      <span className="brand-caption">{caption}</span>
    </div>
  );
}
