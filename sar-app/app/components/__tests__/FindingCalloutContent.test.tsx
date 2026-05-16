/**
 * FindingCalloutContent unit tests.
 *
 * Covers the data → UI shape of the finding-pin preview. The rendering bug
 * the user reported ("tap, flashes for half a second, gone") was actually
 * a Marker-tracksViewChanges issue in the parent, NOT in this component —
 * but pulling the callout body into a pure component lets us pin the
 * data-to-text mapping with a test, so future regressions there get
 * caught before hitting the phone.
 */
import { render, screen } from '@testing-library/react-native';

import FindingCalloutContent from '../FindingCalloutContent';

const baseProps = {
  kind: 'clue' as const,
  description: 'Footprint near switchback',
  confidence: 0.72,
  ts: 1715800000,
  color: '#d4a017',
  glyph: '?',
  timeLabel: '3 min ago',
};

describe('FindingCalloutContent', () => {
  it('renders kind, description, confidence, and time label', () => {
    render(<FindingCalloutContent {...baseProps} />);
    // kind is shown with underscores swapped to spaces
    expect(screen.getByText('clue')).toBeTruthy();
    expect(screen.getByText('Footprint near switchback')).toBeTruthy();
    expect(screen.getByText('Confidence 72% · 3 min ago')).toBeTruthy();
  });

  it('replaces underscore in kind labels (subject_found → "subject found")', () => {
    render(<FindingCalloutContent {...baseProps} kind="subject_found" glyph="✓" />);
    expect(screen.getByText('subject found')).toBeTruthy();
  });

  it('shows a muted placeholder when description is null', () => {
    render(<FindingCalloutContent {...baseProps} description={null} />);
    expect(screen.getByText('No description.')).toBeTruthy();
    // The original description should not appear when null.
    expect(screen.queryByText('Footprint near switchback')).toBeNull();
  });

  it('rounds confidence to a whole percent', () => {
    render(<FindingCalloutContent {...baseProps} confidence={0.337} timeLabel="just now" />);
    expect(screen.getByText('Confidence 34% · just now')).toBeTruthy();
  });

  it('renders the glyph inside the colored swatch', () => {
    render(<FindingCalloutContent {...baseProps} glyph="⚠" />);
    expect(screen.getByText('⚠')).toBeTruthy();
  });

  it('mounts under the documented testID', () => {
    render(<FindingCalloutContent {...baseProps} />);
    expect(screen.getByTestId('finding-callout')).toBeTruthy();
  });
});
