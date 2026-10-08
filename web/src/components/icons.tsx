import type { ComponentType } from "react";
import {
  Bell,
  Cctv,
  ChartColumn,
  ChevronDown,
  ChevronUp,
  ClipboardCheck,
  CreditCard,
  Ellipsis,
  Hexagon,
  History,
  KeyRound,
  Layers,
  LocateFixed,
  LogOut,
  Map as MapGlyph,
  Maximize2,
  Minimize2,
  Minus,
  Monitor,
  Moon,
  PanelBottom,
  PanelLeftClose,
  PanelLeftOpen,
  PictureInPicture2,
  Plus,
  RefreshCw,
  Route,
  Search,
  Settings,
  Sparkles,
  Sun,
  Truck,
  X,
  Zap,
  ZapOff,
  type LucideProps,
} from "lucide-react";

// Single entry point for icons in the app: Lucide (ISC, pinned at 1.46.0 after
// an audit -- no dependencies, no install hooks, no network/eval). Consistent
// stroke and size across the system: changing the visual weight of ALL icons
// means changing ICON_STROKE here. Every consumer imports the same names
// (MapPinIcon, CameraIcon...) -- changing one glyph is a one-line change here,
// and switching icon libraries means touching only this file.
const ICON_STROKE = 1.75;
const DEFAULT_SIZE = 20;

type IconProps = { size?: number; className?: string };

function make(Glyph: ComponentType<LucideProps>, defaultSize = DEFAULT_SIZE) {
  function Icon({ size = defaultSize, className }: IconProps) {
    return <Glyph size={size} strokeWidth={ICON_STROKE} absoluteStrokeWidth={false} className={className} aria-hidden />;
  }
  return Icon;
}

// Navigation
export const MapPinIcon = make(MapGlyph);
export const CameraIcon = make(Cctv);
export const BellIcon = make(Bell);
export const GearIcon = make(Settings);
export const OperationsIcon = make(ClipboardCheck);
export const ReportIcon = make(ChartColumn);
export const BillingIcon = make(CreditCard);
export const TruckIcon = make(Truck);
export const RouteHistoryIcon = make(Route);
export const GeofenceIcon = make(Hexagon);
export const MoreIcon = make(Ellipsis);

// Unit status
export const IgnitionKeyIcon = make(KeyRound, 14);
export function PowerBoltIcon({ size = 14, cut }: { size?: number; cut: boolean }) {
  const Glyph = cut ? ZapOff : Zap;
  return <Glyph size={size} strokeWidth={ICON_STROKE} aria-hidden />;
}

// Controls
export const ChevronDownIcon = make(ChevronDown, 16);
export const SearchIcon = make(Search, 16);
export const CloseIcon = make(X, 16);
export const LayersIcon = make(Layers, 18);
export const LocateIcon = make(LocateFixed, 17);
export const PlusIcon = make(Plus, 17);
export const MinusIcon = make(Minus, 17);
export const RefreshIcon = make(RefreshCw, 16);
export const LogOutIcon = make(LogOut, 17);
export const PanelCloseIcon = make(PanelLeftClose, 17);
export const PanelOpenIcon = make(PanelLeftOpen, 17);
export const HistoryIcon = make(History, 18);
export const PopOutIcon = make(PictureInPicture2, 15);
export const DockIcon = make(PanelBottom, 15);
export const ChevronUpIcon = make(ChevronUp, 16);
export const ExpandIcon = make(Maximize2, 14);
export const ShrinkIcon = make(Minimize2, 14);

// Themes
export const SunIcon = make(Sun, 16);
export const MoonIcon = make(Moon, 16);
export const MonitorIcon = make(Monitor, 16);
export const SparklesIcon = make(Sparkles, 16);
