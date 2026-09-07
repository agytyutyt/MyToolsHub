package com.jztools.infoparse.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.util.zip.ZipInputStream

/** DocxWriter：逐段文本 → 最小 .docx 容器 */
class DocxWriterTest {

    private fun entries(bytes: ByteArray): Map<String, String> {
        val map = mutableMapOf<String, String>()
        ZipInputStream(ByteArrayInputStream(bytes)).use { zip ->
            var e = zip.nextEntry
            while (e != null) {
                map[e.name] = zip.readBytes().toString(Charsets.UTF_8)
                e = zip.nextEntry
            }
        }
        return map
    }

    @Test
    fun `产出为 ZIP 容器且包含 OOXML 最小三件套`() {
        val bytes = DocxWriter.fromText("第一段\n第二段")
        assertEquals("P", bytes[0].toInt().toChar().toString()) // PK 头
        assertEquals("K", bytes[1].toInt().toChar().toString())
        val names = entries(bytes).keys
        assertTrue(names.containsAll(listOf("[Content_Types].xml", "_rels/.rels", "word/document.xml")))
    }

    @Test
    fun `逐行映射为段落并做 XML 转义`() {
        val doc = entries(DocxWriter.fromText("第一行\nA&B<C>\n\n末行"))["word/document.xml"]!!
        assertTrue(doc.contains("<w:t xml:space=\"preserve\">第一行</w:t>"))
        assertTrue(doc.contains("A&amp;B&lt;C&gt;"))
        assertTrue(doc.contains("<w:p/>")) // 空行 → 空段落
        assertTrue(doc.contains("末行"))
        assertEquals(3, Regex("<w:p>").findAll(doc).count()) // 非空段落：第一行 / A&B<C> / 末行
    }

    @Test
    fun `空文本也能产出有效容器`() {
        val map = entries(DocxWriter.fromText(""))
        assertTrue(map.containsKey("word/document.xml"))
        assertTrue(map["word/document.xml"]!!.contains("<w:body>"))
    }
}
