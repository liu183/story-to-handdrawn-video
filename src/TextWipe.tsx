import type {CSSProperties} from 'react';
import {Img, staticFile, useCurrentFrame} from 'remotion';
import {revealProgress} from './easing';

type TextWipeProps = {
  text: string;
  textAsset?: string | null;
  startFrame: number;
  durationFrames: number;
  /** CSS family for the caption face; falls back to the shared hand-font stack. */
  fontFamily?: string;
};

/*
 * Any glyph the chosen caption face lacks falls through to a readable system font
 * instead of rendering as a tofu box.
 */
export const CAPTION_FALLBACKS =
  "'OriginalDiaryHand', 'Microsoft JhengHei', 'Yu Gothic UI', 'Malgun Gothic', sans-serif";

const textStyle = (fontSize: number, fontFamily: string): CSSProperties => ({
  fontFamily,
  fontSize,
  fontWeight: 400,
  lineHeight: 1.34,
  letterSpacing: '0.025em',
  color: '#171714',
  WebkitTextStroke: '0.7px #171714',
  margin: 0,
  maxWidth: 852,
  textAlign: 'left',
  whiteSpace: 'pre-line',
  transform: 'rotate(-0.35deg)',
});

/**
 * Captions are measured in "advance units", one unit per em of the widest glyph.
 * CJK ideographs, kana and hangul syllables each take one full em; Latin letters
 * are roughly half that. Counting raw characters would over-size every Latin line
 * by ~2x and shrink Japanese/Korean ones not at all.
 */
const WIDE = (code: number) =>
  (code >= 0x1100 && code <= 0x115f) ||
  (code >= 0x2e80 && code <= 0xa4cf) ||
  (code >= 0xac00 && code <= 0xd7a3) ||
  (code >= 0xf900 && code <= 0xfaff) ||
  (code >= 0xfe30 && code <= 0xfe6f) ||
  (code >= 0xff00 && code <= 0xff60) ||
  (code >= 0xffe0 && code <= 0xffe6) ||
  (code >= 0x20000 && code <= 0x3fffd);

const advance = (char: string): number => {
  const code = char.codePointAt(0) ?? 0;
  if (WIDE(code)) return 1;
  if (char === ' ') return 0.3;
  if ("iljI.,;:!|'`()[]{}".includes(char)) return 0.28;
  if ('mwMW@%'.includes(char)) return 0.9;
  if ('frstJLT'.includes(char)) return 0.4;
  if (char >= 'A' && char <= 'Z') return 0.68;
  return 0.56;
};

const lineUnits = (line: string) => {
  let total = 0;
  for (const char of line) total += advance(char);
  return total;
};

const fallbackFontSize = (text: string) => {
  const lines = text.split('\n').filter((line) => line.trim().length > 0);
  const lineCount = Math.max(1, lines.length);
  const longestLine = Math.max(...lines.map(lineUnits), 1);
  const widthLimited = Math.floor(850 / (longestLine * 1.08));
  const heightLimited = Math.floor(306 / (lineCount * 1.28));
  return Math.max(48, Math.min(82, widthLimited, heightLimited));
};

export const TextWipe: React.FC<TextWipeProps> = ({
  text,
  textAsset,
  startFrame,
  durationFrames,
  fontFamily,
}) => {
  const frame = useCurrentFrame();
  const progress = revealProgress(frame, startFrame, durationFrames);
  const fontSize = fallbackFontSize(text);
  const captionFont = fontFamily
    ? `${fontFamily}, ${CAPTION_FALLBACKS}`
    : CAPTION_FALLBACKS;

  if (textAsset) {
    return (
      <div
        style={{
          position: 'absolute',
          zIndex: 40,
          top: 86,
          left: 96,
          width: 888,
          height: 288,
          clipPath: `inset(0 ${100 - progress * 100}% 0 0)`,
          overflow: 'hidden',
        }}
      >
        <Img
          src={staticFile(textAsset)}
          style={{
            display: 'block',
            width: '100%',
            height: '100%',
            objectFit: 'contain',
            objectPosition: 'left top',
            filter: 'brightness(1.025) contrast(1.035)',
          }}
        />
      </div>
    );
  }

  return (
    <div
      style={{
        position: 'absolute',
        zIndex: 40,
        top: 92,
        left: 104,
        right: 96,
        display: 'flex',
        justifyContent: 'flex-start',
        clipPath: `inset(0 ${100 - progress * 100}% 0 0)`,
      }}
    >
      <p style={textStyle(fontSize, captionFont)}>{text}</p>
    </div>
  );
};
