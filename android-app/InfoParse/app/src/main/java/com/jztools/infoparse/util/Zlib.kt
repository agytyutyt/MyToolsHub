package com.jztools.infoparse.util

import java.io.ByteArrayOutputStream
import java.util.zip.DataFormatException
import java.util.zip.Inflater

/** zlib 解压工具（v1.7 精简传输压缩信封；Python 端 zlib.compress 产出 RFC1950 zlib 流） */
object Zlib {

    /**
     * 解压 zlib 流数据；损坏或截断时抛 DataFormatException。
     * 循环 inflate 直至 finished；n==0 且输入已耗尽视为截断数据。
     */
    fun inflate(data: ByteArray): ByteArray {
        if (data.isEmpty()) throw DataFormatException("空压缩数据")
        val inflater = Inflater()
        try {
            inflater.setInput(data)
            val out = ByteArrayOutputStream()
            val buf = ByteArray(8192)
            while (!inflater.finished()) {
                val n = inflater.inflate(buf)
                if (n == 0) {
                    if (inflater.needsInput()) throw DataFormatException("压缩数据不完整")
                    continue
                }
                out.write(buf, 0, n)
            }
            return out.toByteArray()
        } finally {
            inflater.end()
        }
    }
}
