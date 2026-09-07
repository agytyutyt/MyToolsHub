package com.jztools.infoparse.protocol

/**
 * GF(2^8) 与 zfec 兼容的 RS 解码（文档附录 C，仅二期缺帧时使用）。
 * 与桌面端 zfec 库（GF(2^8)、本原多项式 0x11d、Vandermonde 系统化矩阵）逐位兼容。
 */
object ZfecCompat {

    private const val PP = 0x11d
    private val EXP = IntArray(512)
    private val LOG = IntArray(256)

    init {
        var x = 1
        for (i in 0 until 255) {
            EXP[i] = x; LOG[x] = i
            x = x shl 1
            if (x and 0x100 != 0) x = x xor PP
        }
        for (i in 255 until 512) EXP[i] = EXP[i - 255]
    }

    private fun gmul(a: Int, b: Int): Int =
        if (a == 0 || b == 0) 0 else EXP[LOG[a] + LOG[b]]

    private fun ginv(a: Int): Int = EXP[255 - LOG[a]]

    /** zfec fec_new 的编码矩阵（前 k 行单位阵，后 m-k 行为校验行） */
    fun buildEncodeMatrix(k: Int, m: Int): Array<IntArray> {
        val V = Array(m) { IntArray(k) }
        V[0][0] = 1
        for (r in 1 until m) for (c in 0 until k) V[r][c] = EXP[(r * c) % 255]
        // 求逆 top k×k（GF 高斯消元）
        val aug = Array(k) { r -> V[r] + IntArray(k) { c -> if (c == r) 1 else 0 } }
        for (col in 0 until k) {
            var piv = col
            while (piv < k && aug[piv][col] == 0) piv++
            if (piv != col) {
                val t = aug[col]; aug[col] = aug[piv]; aug[piv] = t
            }
            val iv = ginv(aug[col][col])
            aug[col] = IntArray(2 * k) { j -> gmul(aug[col][j], iv) }
            for (r in 0 until k) if (r != col && aug[r][col] != 0) {
                val f = aug[r][col]
                aug[r] = IntArray(2 * k) { j -> aug[r][j] xor gmul(f, aug[col][j]) }
            }
        }
        val inv = Array(k) { r -> aug[r].copyOfRange(k, 2 * k) }
        // bottom = V[k:] × inv
        val out = Array(m) { r ->
            if (r < k) IntArray(k) { c -> if (c == r) 1 else 0 }
            else IntArray(k) { j ->
                var acc = 0
                for (c in 0 until k) acc = acc xor gmul(V[r][c], inv[c][j])
                acc
            }
        }
        return out
    }

    /**
     * 解码一组 share：present = share序号(0..m-1) -> 块字节（等长）。
     * 返回 k 个原始块。要求 present.size >= k。
     */
    fun decode(k: Int, m: Int, present: Map<Int, ByteArray>): Array<ByteArray> {
        require(present.size >= k) { "share 不足" }
        val enc = buildEncodeMatrix(k, m)
        val idx = present.keys.sorted().take(k)
        val chunkSize = present.values.first().size
        // k×k 矩阵：行 = 对应 share 的编码行（<k 为单位行）
        val mat = Array(k) { r ->
            val s = idx[r]
            if (s < k) IntArray(k) { c -> if (c == s) 1 else 0 } else enc[s]
        }
        val inv = invert(mat, k)
        // originals = inv × blocks
        val out = Array(k) { ByteArray(chunkSize) }
        for (r in 0 until k) {
            val row = inv[r]
            for (c in 0 until k) {
                val coef = row[c]
                if (coef == 0) continue
                val blk = present.getValue(idx[c])
                for (b in 0 until chunkSize) {
                    out[r][b] = (out[r][b].toInt() xor gmul(coef, blk[b].toInt() and 0xFF)).toByte()
                }
            }
        }
        return out
    }

    /** GF 高斯消元求逆 */
    private fun invert(mat: Array<IntArray>, n: Int): Array<IntArray> {
        val aug = Array(n) { r -> mat[r] + IntArray(n) { c -> if (c == r) 1 else 0 } }
        for (col in 0 until n) {
            var piv = col
            while (piv < n && aug[piv][col] == 0) piv++
            require(piv < n) { "矩阵奇异，share 组合不可逆" }
            if (piv != col) {
                val t = aug[col]; aug[col] = aug[piv]; aug[piv] = t
            }
            val iv = ginv(aug[col][col])
            aug[col] = IntArray(2 * n) { j -> gmul(aug[col][j], iv) }
            for (r in 0 until n) if (r != col && aug[r][col] != 0) {
                val f = aug[r][col]
                aug[r] = IntArray(2 * n) { j -> aug[r][j] xor gmul(f, aug[col][j]) }
            }
        }
        return Array(n) { r -> aug[r].copyOfRange(n, 2 * n) }
    }
}
