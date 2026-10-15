# Generic Analysis

alpha 调用 beta，beta 调用 read_data。read_data 打开输入文件、读取内容、关闭文件，
最后通过 on_ready 回调通知调用方。
