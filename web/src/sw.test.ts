import { describe, expect, it } from "vitest";
import swSource from "../public/sw.js?raw";
import { issueRoute } from "./issues";

// public/sw.js 不经打包，不能 import issueRoute：它按地址的形状认出待审核 issue 的通知（加「通过/打回」），这里核对
// 那个形状与 issueRoute 一致，改了 issue 页的路由这里会失败
const reviewedIssue = new Function(
  `${/function reviewedIssue[\s\S]*?\n}\n/.exec(swSource)![0]}; return reviewedIssue`,
)() as (url: string) => number | null;

describe("sw.js reviewedIssue", () => {
  it("recognizes the issue page and nothing else", () => {
    expect(reviewedIssue(issueRoute(7))).toBe(7);
    expect(reviewedIssue("/sessions/7")).toBeNull();
    expect(reviewedIssue("/notifications")).toBeNull();
    expect(reviewedIssue(`${issueRoute(7)}?review=reject`)).toBeNull();
  });
});
