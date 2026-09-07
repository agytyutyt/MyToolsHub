package com.jztools.infoparse

import com.jztools.infoparse.protocol.Envelope

/** 页面间传递当前信封（内存单例，避免 Intent 序列化大文本） */
object ResultStore {
    @Volatile
    var current: Envelope? = null
}
