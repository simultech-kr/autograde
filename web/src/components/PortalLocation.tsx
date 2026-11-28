import { Fragment, type ComponentPropsWithoutRef } from "react";
import styles from "./portal-location.module.css";

type Destination = {href: string; label: string; onFollow?: () => void};

export function PortalLink({onFollow, ...props}: Omit<ComponentPropsWithoutRef<"a">, "onClick"> & {onFollow?: () => void}) {
  return <a {...props} onClick={event => {
    if (!onFollow || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    onFollow();
  }} />;
}

export function PortalLocation({ancestors, current, backLabel}: {
  ancestors: Destination[]; current: string; backLabel: string;
}) {
  const parent = ancestors[ancestors.length - 1];
  return <div className={styles.locationRow}>
    {parent && <PortalLink className={styles.back} data-parent-link href={parent.href} onFollow={parent.onFollow}>
      <span aria-hidden="true">←</span> {backLabel}
    </PortalLink>}
    <nav className={styles.breadcrumb} aria-label="현재 위치">
      {ancestors.map(({href, label, onFollow}) => <Fragment key={href}>
        <PortalLink href={href} onFollow={onFollow}>{label}</PortalLink><span aria-hidden="true">/</span>
      </Fragment>)}
      <span aria-current="page">{current}</span>
    </nav>
  </div>;
}
