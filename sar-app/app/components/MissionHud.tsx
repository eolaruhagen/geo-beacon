/**
 * MissionHud — presentational wrapper around BroadcastBanner +
 * CurrentOrdersCard.
 *
 * The hooks (useMe, useRoute), action handlers, and force-open toggle
 * state were lifted up to mission.tsx so the top-right button stack
 * (bell + clipboard + dev dispatch + finding) can live in the same flex
 * row as the title pill instead of free-floating absolute. That kept the
 * layout from cooking when multiple top overlays competed for the same
 * pixels.
 *
 * Parent passes `topOffsetPx` (computed from useSafeAreaInsets + the
 * title pill height) so the banner tucks just below the pill on any
 * device — notched / non-notched / flat.
 */
import { useCallback } from 'react';

import BroadcastBanner from './BroadcastBanner';
import CurrentOrdersCard from './CurrentOrdersCard';
import type { MeResponse } from '../lib/api';

type Props = {
  me: MeResponse | null;
  broadcastForceOpen: boolean;
  ordersForceOpen: boolean;
  closeBroadcastManual: () => void;
  closeOrdersManual: () => void;
  onAck: () => void;
  onStart: () => void;
  onComplete: (notes?: string) => void;
  actionBusy: boolean;
  /** Vertical offset (px) where the banner should start — used to clear
   *  the title pill above. Derived from useSafeAreaInsets in the parent. */
  topOffsetPx: number;
};

export default function MissionHud({
  me,
  broadcastForceOpen,
  ordersForceOpen,
  closeBroadcastManual,
  closeOrdersManual,
  onAck,
  onStart,
  onComplete,
  actionBusy,
  topOffsetPx,
}: Props) {
  const ad = me?.active_dispatch ?? null;
  const broadcasts = me?.recent_broadcasts ?? [];
  const segmentName =
    me?.segment_geojson?.properties?.name ??
    (ad ? `Dispatch #${ad.id}` : null);

  // Wrap onComplete so we accept the optional notes arg from the card.
  const handleComplete = useCallback(
    (notes?: string) => onComplete(notes),
    [onComplete],
  );

  return (
    <>
      <BroadcastBanner
        broadcasts={broadcasts}
        forceOpen={broadcastForceOpen}
        onCloseManual={closeBroadcastManual}
        topOffsetPx={topOffsetPx}
      />

      <CurrentOrdersCard
        active={ad}
        segmentName={segmentName}
        forceOpen={ordersForceOpen}
        onCloseManual={closeOrdersManual}
        onAck={onAck}
        onStart={onStart}
        onComplete={handleComplete}
        busy={actionBusy}
      />
    </>
  );
}
