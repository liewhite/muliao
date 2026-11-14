/**
 * 语句式 switch 的 default 分支：参数类型是 never，联合类型多出一种而 switch 没跟上时编译不过；万一运行到这里（后端比
 * 前端新），当场报错，不静默跳过。返回式 switch 不用它：tsconfig 开了 noImplicitReturns，漏一种同样编译不过。
 */
export function assertNever(value: never): never {
  throw new Error(`unexpected variant: ${JSON.stringify(value)}`)
}
