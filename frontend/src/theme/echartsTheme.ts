/**
 * Tessallite ECharts Theme — Enterprise-grade KPI visualisation theme
 * matching the Tessallite brand identity.
 *
 * Registered once at app startup via registerTessalliteEChartsTheme().
 */
import * as echarts from "echarts/core";
import { palette, font, ui } from "./tokens";

const ECHARTS_THEME_NAME = "tessallite";

/** Axis split-line colour — very subtle on mint/light backgrounds */
const SPLIT_LINE = "#E8ECE9";

const TESSALLITE_ECHARTS_THEME: Record<string, unknown> = {
  color: [
    palette.primaryGreen,
    "#34A069",
    "#D4AF37",
    "#7B3FA0",
    "#3A5EA8",
    "#B8960C",
    "#4A1870",
    "#1A2D5A",
  ],

  backgroundColor: "transparent",

  textStyle: {
    fontFamily: font.sans,
    color: palette.textSecondary,
    fontSize: 11,
  },

  title: {
    textStyle: {
      fontFamily: font.sans,
      fontWeight: 600,
      fontSize: 14,
      color: palette.charcoal,
    },
    subtextStyle: {
      fontFamily: font.sans,
      fontWeight: 400,
      fontSize: 11,
      color: palette.textSecondary,
    },
  },

  legend: {
    textStyle: {
      fontFamily: font.sans,
      fontWeight: 500,
      color: palette.textSecondary,
    },
  },

  tooltip: {
    backgroundColor: palette.white,
    borderColor: palette.slateBorder,
    borderWidth: 1,
    textStyle: {
      fontFamily: font.sans,
      color: palette.charcoal,
      fontSize: 12,
    },
    extraCssText:
      "border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); padding: 8px 12px;",
  },

  categoryAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: {
      fontFamily: font.sans,
      fontWeight: 500,
      fontSize: 10,
      color: palette.textSecondary,
    },
    splitLine: { show: false },
  },
  valueAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: {
      fontFamily: font.sans,
      fontWeight: 500,
      fontSize: 10,
      color: palette.textSecondary,
    },
    splitLine: {
      lineStyle: { color: SPLIT_LINE, width: 1, type: "dashed" },
    },
  },
  logAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: {
      fontFamily: font.sans,
      fontWeight: 500,
      fontSize: 10,
      color: palette.textSecondary,
    },
    splitLine: {
      lineStyle: { color: SPLIT_LINE, width: 1, type: "dashed" },
    },
  },
  timeAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: {
      fontFamily: font.sans,
      fontWeight: 500,
      fontSize: 10,
      color: palette.textSecondary,
    },
    splitLine: { show: false },
  },

  radar: {
    axisLine: { lineStyle: { color: palette.slateBorder } },
    splitLine: { lineStyle: { color: SPLIT_LINE } },
    splitArea: null,
    axisLabel: {
      fontFamily: font.sans,
      color: palette.textSecondary,
    },
  },

  gauge: {
    axisLine: {
      lineStyle: {
        width: 16,
      },
    },
    axisTick: {
      distance: -16,
      length: 6,
      lineStyle: { color: palette.textSecondary, width: 1 },
    },
    splitLine: {
      distance: -16,
      length: 12,
      lineStyle: { color: palette.textSecondary, width: 1.5 },
    },
    axisLabel: {
      fontFamily: font.sans,
      fontSize: 9,
      fontWeight: 500,
      color: palette.textSecondary,
    },
    anchor: {
      show: true,
      showAbove: true,
      size: 14,
      itemStyle: {
        borderWidth: 2,
        borderColor: palette.charcoal,
      },
    },
    title: {
      fontFamily: font.sans,
      fontSize: 11,
      fontWeight: 500,
      color: palette.textSecondary,
    },
    detail: {
      fontFamily: font.sans,
      fontWeight: 700,
      fontSize: 20,
      color: palette.charcoal,
      offsetCenter: [0, "45%"],
      valueAnimation: true,
    },
  },

  bar: {
    itemStyle: {
      borderRadius: [4, 4, 0, 0],
    },
    barWidth: "60%",
  },

  line: {
    itemStyle: { borderWidth: 2 },
    lineStyle: { width: 2.5 },
    symbolSize: 6,
    symbol: "circle",
    smooth: true,
  },

  scatter: {
    itemStyle: {
      borderWidth: 2,
      borderColor: palette.white,
    },
  },

  pie: {
    itemStyle: {
      borderColor: palette.white,
      borderWidth: 2,
    },
    label: {
      fontFamily: font.sans,
      fontWeight: 500,
      fontSize: 11,
      color: palette.textSecondary,
    },
  },

  markPoint: {
    label: {
      fontFamily: font.sans,
      fontWeight: 600,
      color: palette.charcoal,
    },
  },
};

function registerTessalliteEChartsTheme(): void {
  echarts.registerTheme(ECHARTS_THEME_NAME, TESSALLITE_ECHARTS_THEME);
}

export {
  registerTessalliteEChartsTheme,
  ECHARTS_THEME_NAME,
  TESSALLITE_ECHARTS_THEME,
};
