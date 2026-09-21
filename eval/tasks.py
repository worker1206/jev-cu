"""mini benchmark 任务集：每任务=起点URL+中文指令+期望动作关键元素的文本特征"""
TASKS = [
 {"id":"T1","url":"https://example.com","task":"了解更多关于这个域名的信息","expect":"More information","expect_tag":"a"},
 {"id":"T2","url":"https://www.wikipedia.org","task":"在维基百科搜索“人工智能”","expect":"搜索","expect_tag":"input"},
 {"id":"T3","url":"https://httpbin.org/forms/post","task":"填写 pizza 下单表单：先点 Custname 输入框","expect":"Custname","expect_tag":"input"},
 {"id":"T4","url":"https://the-internet.herokuapp.com/login","task":"登录该测试站点，先点用户名输入框","expect":"username","expect_tag":"input"},
 {"id":"T5","url":"https://the-internet.herokuapp.com/login","task":"登录该测试站点，点击登录按钮","expect":"Login","expect_tag":"button"},
 {"id":"T6","url":"https://www.bing.com","task":"在必应搜索“深度学习”","expect":"搜索","expect_tag":"input"},
 {"id":"T7","url":"https://github.com","task":"在 GitHub 全站搜索仓库 jev","expect":"搜索","expect_tag":"input"},
 {"id":"T8","url":"https://www.baidu.com","task":"在百度搜索“TypeSafe AI”","expect":"百度一下","expect_tag":"submit"},
]
